# coding=utf-8
"""Reddit source class.

Reddit API docs:
https://github.com/reddit-archive/reddit/wiki/API
https://www.reddit.com/dev/api
https://www.reddit.com/prefs/apps

PRAW API docs:
https://praw.readthedocs.io/
"""
import urllib.parse, urllib.request
import threading

from cachetools import cachedmethod, TTLCache
from oauth_dropins import reddit
from oauth_dropins.webutil import util
import praw
from prawcore.exceptions import NotFound

from . import source

USER_CACHE_TIME = 5 * 60  # 5 minute expiration, in seconds
user_cache = TTLCache(1000, USER_CACHE_TIME)
user_cache_lock = threading.RLock()


class Reddit(source.Source):
  """Reddit source class. See file docstring and Source class for details."""

  DOMAIN = 'reddit.com'
  BASE_URL = 'https://reddit.com'
  NAME = 'Reddit'
  OPTIMIZED_COMMENTS = True

  def __init__(self, refresh_token):
    self.refresh_token = refresh_token
    self.reddit_api = None

  def get_reddit_api(self):
    if not self.reddit_api:
      self.reddit_api = praw.Reddit(client_id=reddit.REDDIT_APP_KEY,
                                    client_secret=reddit.REDDIT_APP_SECRET,
                                    refresh_token=self.refresh_token,
                                    user_agent='granary (https://granary.io/)')
      self.reddit_api.read_only = True

    return self.reddit_api

  @classmethod
  def post_id(self, url):
    """Guesses the post id of the given URL.

    Args:
      url: string

    Returns:
      string, or None
    """
    path_parts = urllib.parse.urlparse(url).path.rstrip('/').split('/')
    if len(path_parts) >= 2:
      return path_parts[-2]

  @cachedmethod(lambda self: user_cache, lock=lambda self: user_cache_lock,
                key=lambda user: getattr(user, 'name', None))
  def praw_to_actor(self, praw_user):
    """Converts a PRAW Redditor to an actor.

    Makes external calls to fetch data from the Reddit API.

    https://praw.readthedocs.io/en/latest/code_overview/models/redditor.html

    Caches fetched user data for 5m to avoid repeating user profile API requests
    when fetching multiple comments or posts from the same author. Background:
    https://github.com/snarfed/bridgy/issues/1021

    Ideally this would be part of PRAW, but they seem uninterested:
    https://github.com/praw-dev/praw/issues/131
    https://github.com/praw-dev/praw/issues/1140

    Args:
      user: PRAW Redditor object

    Returns:
      an ActivityStreams actor dict, ready to be JSON-encoded
    """
    try:
      user = reddit.praw_to_user(praw_user)
    except NotFound:
      return {}

    return self.user_to_actor(user)

  def user_to_actor(self, user):
    """Converts a dict user to an actor.

    Args:
      user: JSON user

    Returns:
      an ActivityStreams actor dict, ready to be JSON-encoded
    """
    username = user.get('name')
    if not username:
      return {}

    # trying my best to grab all the urls from the profile description
    urls = [f'{self.BASE_URL}/user/{username}/']
    description = None

    subreddit = user.get('subreddit')
    if subreddit:
      url = subreddit.get('url')
      if url:
        urls.append(self.BASE_URL + url)
      description = subreddit.get('description')
      urls += util.trim_nulls(util.extract_links(description))

    image = user.get('icon_img')

    return util.trim_nulls({
      'objectType': 'person',
      'displayName': username,
      'image': {'url': image},
      'id': self.tag_uri(username),
      # numeric_id is our own custom field that always has the source's numeric
      # user id, if available.
      'numeric_id': user.get('id'),
      'published': util.maybe_timestamp_to_iso8601(user.get('created_utc')),
      'url': urls[0],
      'urls': [{'value': u} for u in urls] if len(urls) > 1 else None,
      'username': username,
      'description': description,
    })

  def praw_to_object(self, thing, type):
    """Converts a PRAW object to an AS1 object.

    Currently only returns public content.

    Note that this will make external API calls to lazily load some attributes.

    Args:
      thing: a PRAW object, Submission or Comment
      type: string to denote whether to get submission or comment content

    Returns:
      an ActivityStreams object dict, ready to be JSON-encoded
      """
    id = getattr(thing, 'id', None)
    if not id:
      return {}

    published = util.maybe_timestamp_to_iso8601(getattr(thing, 'created_utc', None))
    obj = {
      'id': self.tag_uri(id),
      'url': self.BASE_URL + thing.permalink,
      'published': published,
      'to': [{
        'objectType': 'group',
        'alias': '@public',
      }],
    }

    user = getattr(thing, 'author', None)
    if user:
      obj['author'] = self.praw_to_actor(user)

    if type == 'submission':
      content = getattr(thing, 'selftext', None)
      obj.update({
        'displayName': getattr(thing, 'title', None),
        'content': content,
        'objectType': 'note',
        'tags': [{
          'objectType': 'article',
          'url': t,
          'displayName': t,
        } for t in util.extract_links(content)],
      })

      url = getattr(thing, 'url', None)
      if url:
        obj.update({
          'objectType': 'bookmark',
          'targetUrl': url,
        })

    elif type == 'comment':
      obj.update({
        'content': getattr(thing, 'body_html', None),
        'objectType': 'comment',
      })
      reply_to = thing.parent()
      if reply_to:
        obj['inReplyTo'] = [{
          'id': self.tag_uri(getattr(reply_to, 'id', None)),
          'url': self.BASE_URL + getattr(reply_to, 'permalink', None),
        }]

    return self.postprocess_object(obj)

  def praw_to_activity(self, thing, type):
    """Converts a PRAW submission or comment to an activity.

    Note that this will make external API calls to lazily load some attributes.

    https://praw.readthedocs.io/en/latest/code_overview/models/submission.html
    https://praw.readthedocs.io/en/latest/code_overview/models/comment.html

    Args:
      thing: a PRAW object, Submission or Comment
      type: string to denote whether to get submission or comment content

    Returns:
      an ActivityStreams activity dict, ready to be JSON-encoded
    """
    obj = self.praw_to_object(thing, type)
    if not obj:
      return {}

    activity = {
      'verb': 'post',
      'id': obj['id'],
      'url': self.BASE_URL + getattr(thing, 'permalink', None),
      'actor': obj.get('author'),
      'object': obj,
    }
    return self.postprocess_activity(activity)

  def _fetch_replies(self, r, activities):
    """Fetches and injects comments into a list of activities, in place.

    limitations: Only includes top level comments
    Args:
      r: PRAW API object for querying submissions in activities
      activities: list of activity dicts
    """
    for activity in activities:
      subm = r.submission(id=util.parse_tag_uri(activity.get('id'))[1])

      # for v0 we will use just the top level comments because threading is hard.
      # feature request: https://github.com/snarfed/bridgy/issues/1014
      subm.comments.replace_more()
      replies = [
          self.praw_to_activity(top_level_comment, 'comment')
          for top_level_comment in subm.comments
      ]
      items = [r.get('object') for r in replies]
      activity['object']['replies'] = {
        'items': items,
        'totalItems': len(items),
      }

  def get_activities_response(self, user_id=None, group_id=None, app_id=None,
                              activity_id=None, start_index=0, count=0,
                              etag=None, min_id=None, cache=None,
                              fetch_replies=False, fetch_likes=False,
                              fetch_shares=False, fetch_events=False,
                              fetch_mentions=False, search_query=None, **kwargs):
    """Fetches submissions and ActivityStreams activities.

    Currently only implements activity_id, search_query and fetch_replies.
    """
    activities = []
    r = self.get_reddit_api()

    if activity_id:
      subm = r.submission(id=activity_id)
      activities.append(self.praw_to_activity(subm, 'submission'))
    elif search_query:
      sr = r.subreddit('all')
      subms = sr.search(search_query, sort='new')
      activities.extend([self.praw_to_activity(subm, 'submission') for subm in subms])

    if fetch_replies:
      self._fetch_replies(r, activities)

    return self.make_activities_base_response(activities)

  def get_actor(self, user_id=None):
    """PLACEHOLDER. Returns an empty dict.

    Only here because the granary.io API needs this to emit Atom data.

    TODO: implement.
    """
    return {}

  def get_comment(self, comment_id, activity_id=None, activity_author_id=None,
                  activity=None):
    """Returns an ActivityStreams comment object.

    Args:
      comment_id: string comment id
      activity_id: string activity id, Ignored
      activity_author_id: string activity author id. Ignored.
      activity: activity object, Ignored
    """
    r = self.get_reddit_api()
    return self.praw_to_object(r.comment(id=comment_id), 'comment')

  def user_url(self, username):
    """Returns the Reddit URL for a given user."""
    return 'https://%s/user/%s' % (self.DOMAIN, username)
