"""Instagram source class.

Instagram's API doesn't tell you if a user has marked their account private or
not, so the Audience Targeting 'to' field is currently always set to @public.
http://help.instagram.com/448523408565555
https://groups.google.com/forum/m/#!topic/instagram-api-developers/DAO7OriVFsw
https://groups.google.com/forum/#!searchin/instagram-api-developers/private
"""
import datetime
import logging
import operator
import re
import string
import urllib.parse, urllib.request
import xml.sax.saxutils

from oauth_dropins.webutil import util
from oauth_dropins.webutil.util import json_dumps, json_loads
import requests

from . import source

# Maps Instagram media type to ActivityStreams objectType.
OBJECT_TYPES = {'image': 'photo', 'video': 'video'}

API_USER_URL = 'https://api.instagram.com/v1/users/%s'
API_USER_MEDIA_URL = 'https://api.instagram.com/v1/users/%s/media/recent'
API_USER_FEED_URL = 'https://api.instagram.com/v1/users/self/feed'
API_USER_LIKES_URL = 'https://api.instagram.com/v1/users/%s/media/liked'
API_MEDIA_URL = 'https://api.instagram.com/v1/media/%s'
API_MEDIA_SEARCH_URL = 'https://api.instagram.com/v1/tags/%s/media/recent'
API_MEDIA_SHORTCODE_URL = 'https://api.instagram.com/v1/media/shortcode/%s'
API_MEDIA_POPULAR_URL = 'https://api.instagram.com/v1/media/popular'
API_MEDIA_LIKES_URL = 'https://api.instagram.com/v1/media/%s/likes'
API_COMMENT_URL = 'https://api.instagram.com/v1/media/%s/comments'

HTML_BASE_URL = util.read('instagram_scrape_base') or 'https://www.instagram.com/'
HTML_MEDIA = HTML_BASE_URL + 'p/%s/'
HTML_PROFILE = HTML_BASE_URL + '%s/'
HTML_PRELOAD_RE = re.compile(
  r'^/graphql/query/\?query_hash=[^&]*&(amp;)?variables=(%7B%7D|{})$')
# the query hash here comes (i think) from inside a .js file served by IG, so
# we'd have to fetch and scrape that to get it dynamically. not worth it yet.
HTML_LIKES_URL = HTML_BASE_URL + 'graphql/query/?query_hash=d5d763b1e2acf209d62d22d184488e57&variables={"shortcode":"%s","include_reel":false,"first":100}'
HTML_DATA_RE = re.compile(r"""
  <script\ type="text/javascript">
  window\.(_sharedData\ =|__additionalDataLoaded\('[^']+',)\ *
  (.+?)
  \)?;</script>""", re.VERBOSE)
HTML_USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:82.0) Gecko/20100101 Firefox/82.0'

# URL-safe base64 encoding. used in Instagram.id_to_shortcode()
BASE64 = string.ascii_uppercase + string.ascii_lowercase + string.digits + '-_'

MENTION_RE = re.compile(r'@([A-Za-z0-9._]+)')

# global lock for backing off scraping due to rate limiting.
RATE_LIMIT_BACKOFF = datetime.timedelta(seconds=5 * 60)
_last_rate_limited = None      # datetime
_last_rate_limited_exc = None  # requests.HTTPError

AUTO_ALT_TEXT_PREFIXES = (
  'No photo description available.',
  'Image may contain: ',
)


class Instagram(source.Source):
  """Instagram source class. See file docstring and Source class for details."""

  DOMAIN = 'instagram.com'
  BASE_URL = 'https://www.instagram.com/'
  NAME = 'Instagram'
  FRONT_PAGE_TEMPLATE = 'templates/instagram_index.html'
  OPTIMIZED_COMMENTS = False

  EMBED_POST = """
  <script async defer src="//platform.instagram.com/en_US/embeds.js"></script>
  <blockquote class="instagram-media" data-instgrm-captioned data-instgrm-version="4"
              style="margin: 0 auto; width: 100%%">
    <p><a href="%(url)s" target="_top">%(content)s</a></p>
  </blockquote>
  """

  def __init__(self, access_token=None, allow_comment_creation=False,
               scrape=False, cookie=None):
    """Constructor.

    If an OAuth access token is provided, it will be passed on to Instagram.
    This will be necessary for some people and contact details, based on their
    privacy settings.

    Args:
      access_token: string, optional OAuth access token
      allow_comment_creation: boolean, optionally disable comment creation,
        useful if the app is not approved to create comments.
      scrape: boolean, whether to scrape instagram.com's HTML (True) or use
        the API (False)
      cookie: string, optional sessionid cookie to use when scraping.
    """
    self.access_token = access_token
    self.allow_comment_creation = allow_comment_creation
    self.scrape = scrape
    self.cookie = cookie

  def urlopen(self, url, **kwargs):
    """Wraps :func:`urllib2.urlopen()` and passes through the access token."""
    if self.access_token:
      # TODO add access_token to the data parameter for POST requests
      url = util.add_query_params(url, [('access_token', self.access_token)])
    resp = util.urlopen(urllib.request.Request(url, **kwargs))
    return (resp if kwargs.get('data')
            else source.load_json(resp.read(), url).get('data'))

  @classmethod
  def user_url(cls, username):
    if username:
      return '%s%s/' % (cls.BASE_URL, username)

  @classmethod
  def media_url(cls, shortcode):
    if shortcode:
      return '%sp/%s/' % (cls.BASE_URL, shortcode)

  def get_actor(self, user_id=None, **kwargs):
    """Returns a user as a JSON ActivityStreams actor dict.

    Args:
      user_id: string id or username. Defaults to 'self', ie the current user.
      kwargs: if scraping, passed through to get_activities_response(). Raises
        AssertionError if provided and not scraping.
    """
    if user_id is None:
      if self.scrape:
        return {}
      user_id = 'self'

    if not self.scrape:
      return self.user_to_actor(util.trim_nulls(
        self.urlopen(API_USER_URL % user_id) or {}))

    resp = self.get_activities_response(
      group_id=source.SELF, user_id=user_id, **kwargs)
    items = resp.get('items')
    return items[0].get('actor') if items else {}

  def get_activities_response(self, user_id=None, group_id=None, app_id=None,
                              activity_id=None, start_index=0, count=0,
                              etag=None, min_id=None, cache=None,
                              fetch_replies=False, fetch_likes=False,
                              fetch_shares=False, fetch_events=False,
                              fetch_mentions=False, search_query=None,
                              scrape=False, cookie=None, ignore_rate_limit=False,
                              **kwargs):
    """Fetches posts and converts them to ActivityStreams activities.

    See method docstring in source.py for details. app_id is ignored.
    Supports min_id, but not ETag, since Instagram doesn't support it.

    http://instagram.com/developer/endpoints/users/#get_users_feed
    http://instagram.com/developer/endpoints/users/#get_users_media_recent

    Likes are always included, regardless of the fetch_likes kwarg. They come
    bundled in the 'likes' field of the API Media object:
    http://instagram.com/developer/endpoints/media/#

    Mentions are never fetched or included because the API doesn't support
    searching for them.
    https://github.com/snarfed/bridgy/issues/523#issuecomment-155523875

    Shares are never fetched included since there is no share feature.

    Instagram only supports search over hashtags, so if search_query is set, it
    must begin with #.

    May populate a custom 'ig_like_count' property in media objects. (Currently
    only when scraping.)

    Args:
      scrape: if True, scrapes HTML from instagram.com instead of using the API.
        Populates the user's actor object in the 'actor' response field.
        Useful for apps that haven't yet been approved in the new permissions
        approval process. Currently only supports group_id=SELF. Also supports
        passing a shortcode as activity_id as well as the internal API id.
        http://developers.instagram.com/post/133424514006/instagram-platform-update
      cookie: string, only used if scrape=True
      ignore_rate_limit: boolean, for scraping, always make an HTTP request,
        even if we've been rate limited recently
      **: see :meth:`Source.get_activities_response`
    """
    if group_id is None:
      group_id = source.FRIENDS

    if scrape or self.scrape:
      cookie = cookie or self.cookie
      if not (activity_id or
              (group_id == source.SELF and user_id) or
              (group_id == source.FRIENDS and cookie)):
        raise NotImplementedError(
          'Scraping only supports activity_id, user_id and group_id=@self, or cookie and group_id=@friends.')
      elif fetch_likes and not cookie:
        raise NotImplementedError('Scraping likes requires a cookie.')

      # cache rate limited responses and short circuit
      global _last_rate_limited, _last_rate_limited_exc
      now = datetime.datetime.now()
      if not ignore_rate_limit and _last_rate_limited:
        retry = _last_rate_limited + RATE_LIMIT_BACKOFF
        if now < retry:
          logging.info('Remembered rate limit at %s, waiting until %s to try again.',
                       _last_rate_limited, retry)
          assert _last_rate_limited_exc
          raise _last_rate_limited_exc

      try:
        return self._scrape(
          user_id=user_id, group_id=group_id, activity_id=activity_id, count=count,
          cookie=cookie, fetch_extras=fetch_replies or fetch_likes, cache=cache)
      except Exception as e:
        code, body = util.interpret_http_exception(e)
        if not ignore_rate_limit and code in ('302', '401', '429', '503'):
          logging.info('Got rate limited! Remembering for %s', str(RATE_LIMIT_BACKOFF))
          _last_rate_limited = now
          _last_rate_limited_exc = e
        raise

    if user_id is None:
      user_id = 'self'

    if search_query:
      if search_query.startswith('#'):
        search_query = search_query[1:]
      else:
        raise ValueError(
          'Instagram only supports search over hashtags, so search_query must '
          'begin with the # character.')

    # TODO: paging
    media = []
    kwargs = {}
    if min_id is not None:
      kwargs['min_id'] = min_id

    activities = []
    try:
      media_url = (API_MEDIA_URL % activity_id if activity_id else
                   API_USER_MEDIA_URL % user_id if group_id == source.SELF else
                   API_MEDIA_POPULAR_URL if group_id == source.ALL else
                   API_MEDIA_SEARCH_URL % search_query if group_id == source.SEARCH else
                   API_USER_FEED_URL if group_id == source.FRIENDS else None)
      assert media_url
      media = self.urlopen(util.add_query_params(media_url, kwargs))
      if media:
        if activity_id:
          media = [media]
        activities += [self.media_to_activity(m) for m in util.trim_nulls(media)]

      if group_id == source.SELF and fetch_likes:
        # add the user's own likes
        liked = self.urlopen(
          util.add_query_params(API_USER_LIKES_URL % user_id, kwargs))
        if liked:
          user = self.urlopen(API_USER_URL % user_id)
          activities += [self.like_to_object(user, l['id'], l['link'])
                         for l in liked]

    except urllib.error.HTTPError as e:
      code, body = util.interpret_http_exception(e)
      # instagram api should give us back a json block describing the
      # error. but if it's an error for some other reason, it probably won't
      # be properly formatted json.
      try:
        body_obj = json_loads(body) if body else {}
      except ValueError:
        body_obj = {}

      if body_obj.get('meta', {}).get('error_type') == 'APINotFoundError':
        logging.warning(body_obj.get('meta', {}).get('error_message'), exc_info=True)
      else:
        raise e

    return self.make_activities_base_response(activities)

  def _scrape(self, user_id=None, group_id=None, activity_id=None, cookie=None,
              count=None, fetch_extras=False, cache=None, shortcode=None):
    """Scrapes a user's profile or feed and converts the media to activities.

    Args:
      user_id: string
      activity_id: string, e.g. '1020355224898358984_654594'
      count: integer, number of activities to fetch and return, None for all
      fetch_extras: boolean
      cookie: string
      shortcode: string, e.g. '4pB6vEx87I'

    Returns:
      dict activities API response
    """
    cookie = cookie or self.cookie
    assert user_id or activity_id or shortcode or cookie
    assert not (activity_id and shortcode)

    if not shortcode:
      shortcode = self.id_to_shortcode(activity_id)

    url = (HTML_MEDIA % shortcode if shortcode
           else HTML_PROFILE % user_id if user_id and group_id == source.SELF
           else HTML_BASE_URL)
    get_kwargs = {'allow_redirects': False}
    if cookie:
      if not cookie.startswith('sessionid='):
        cookie = 'sessionid=' + cookie
      get_kwargs['headers'] = {'Cookie': cookie, 'User-Agent': HTML_USER_AGENT}

    resp = util.requests_get(url, **get_kwargs)
    location = resp.headers.get('Location', '')
    if ((cookie and 'not-logged-in' in resp.text) or
        (resp.status_code in (301, 302) and
         ('/accounts/login' in location or '/challenge/' in location))):
      resp.status_code = 401
      raise requests.HTTPError('401 Unauthorized', response=resp)
    elif resp.status_code == 404:
      if activity_id:
        return self._scrape(shortcode=activity_id, cookie=cookie, count=count)
      # otherwise not found, fall through and return empty response
    else:
      resp.raise_for_status()

    activities, actor = self.scraped_to_activities(resp.text, cookie=cookie,
                                                   count=count)

    if fetch_extras:
      # batch get cached counts of comments and likes for all activities
      cached = {}
      # don't update the cache until the end, in case we hit an error before
      cache_updates = {}
      if cache is not None:
        keys = []
        for activity in activities:
          _, id = util.parse_tag_uri(activity['id'])
          keys.extend(['AIL ' + id, 'AIC ' + id])
        cached = cache.get_multi(keys)

      for i, activity in enumerate(activities):
        obj = activity['object']
        _, id = util.parse_tag_uri(activity['id'])
        likes = obj.get('ig_like_count') or 0
        comments = obj.get('replies', {}).get('totalItems') or 0
        likes_key = 'AIL %s' % id
        comments_key = 'AIC %s' % id

        if (likes and likes != cached.get(likes_key) or
            comments and comments != cached.get(comments_key)):
          if not activity_id and not shortcode:
            url = activity['url'].replace(self.BASE_URL, HTML_BASE_URL)
            resp = util.requests_get(url, **get_kwargs)
            resp.raise_for_status()
          # otherwise resp is a fetch of just this activity; reuse it

          full_activity, _ = self.scraped_to_activities(
            resp.text, cookie=cookie, count=count, fetch_extras=fetch_extras)
          if full_activity:
            activities[i] = full_activity[0]
            cache_updates.update({likes_key: likes, comments_key: comments})

      if cache_updates and cache is not None:
        cache.set_multi(cache_updates)

    resp = self.make_activities_base_response(activities)
    resp['actor'] = actor
    return resp

  def get_comment(self, comment_id, activity_id=None, activity_author_id=None,
                  activity=None):
    """Returns an ActivityStreams comment object.

    Args:
      comment_id: string comment id
      activity_id: string activity id, optional
      activity_author_id: string activity author id. Ignored.
      activity: activity object, optional. Avoids fetching the activity.
    """
    if not activity:
      activity = self._get_activity(None, activity_id)
    if activity:
      # TODO: unify with instagram, maybe in source.get_comment()
      tag_id = self.tag_uri(comment_id)
      for reply in activity.get('object', {}).get('replies', {}).get('items', []):
        if reply.get('id') == tag_id:
          return reply

  def get_share(self, activity_user_id, activity_id, share_id, activity=None):
    """Not implemented. Returns None. Resharing isn't a feature of Instagram.
    """
    return None

  def create(self, obj, include_link=source.OMIT_LINK, ignore_formatting=False):
    """Creates a new comment or like.

    Args:
      obj: ActivityStreams object
      include_link: string
      ignore_formatting: boolean

    Returns:
      a CreationResult. if successful, content will have and 'id' and
             'url' keys for the newly created Instagram object
    """
    return self._create(obj, include_link=include_link, preview=False,
                        ignore_formatting=ignore_formatting)

  def preview_create(self, obj, include_link=source.OMIT_LINK,
                     ignore_formatting=False):
    """Preview a new comment or like.

    Args:
      obj: ActivityStreams object
      include_link: string
      ignore_formatting: boolean

    Returns:
      a CreationResult. if successful, content and description
             will describe the new instagram object.
    """
    return self._create(obj, include_link=include_link, preview=True,
                        ignore_formatting=ignore_formatting)

  def _create(self, obj, include_link=source.OMIT_LINK, preview=None,
              ignore_formatting=False):
    """Creates a new comment or like.

    The OAuth access token must have been created with scope=comments+likes (or
    just one, respectively).
    http://instagram.com/developer/authentication/#scope

    To comment, you need to apply for access:
    https://docs.google.com/spreadsheet/viewform?formkey=dFNydmNsUUlEUGdySWFWbGpQczdmWnc6MQ

    http://instagram.com/developer/endpoints/comments/#post_media_comments
    http://instagram.com/developer/endpoints/likes/#post_likes

    Args:
      obj: ActivityStreams object
      include_link: string
      preview: boolean

    Returns:
      a CreationResult. if successful, content will have and 'id' and
             'url' keys for the newly created Instagram object
    """
    # TODO: validation, error handling
    type = obj.get('objectType')
    verb = obj.get('verb')
    base_obj = self.base_object(obj)
    base_id = base_obj.get('id')
    base_url = base_obj.get('url')

    logging.debug(
      'instagram create request with type=%s, verb=%s, id=%s, url=%s',
      type, verb, base_id, base_url)

    if type == 'comment':
      # most applications are not approved by instagram to create comments;
      # better to give a useful error message than try and fail.
      if not self.allow_comment_creation:
        return source.creation_result(
          abort=True,
          error_plain='Cannot publish comments on Instagram',
          error_html='<a href="http://instagram.com/developer/endpoints/comments/#post_media_comments">Cannot publish comments</a> on Instagram. The Instagram API technically supports creating comments, but <a href="http://stackoverflow.com/a/26889101/682648">anecdotal</a> <a href="http://stackoverflow.com/a/20229275/682648">evidence</a> suggests they are very selective about which applications they approve to do so.')
      content = self._content_for_create(obj)
      if preview:
        return source.creation_result(
          content=content,
          description='<span class="verb">comment</span> on <a href="%s">'
                      'this post</a>:\n%s' % (base_url, self.embed_post(base_obj)))

      self.urlopen(API_COMMENT_URL % base_id, data=urllib.parse.urlencode({
        'access_token': self.access_token,
        'text': content,
      }))
      # response will be empty even on success, see
      # http://instagram.com/developer/endpoints/comments/#post_media_comments.
      # TODO where can we get the comment id?
      obj = self.comment_to_object({}, base_id, None)
      return source.creation_result(obj)

    elif type == 'activity' and verb == 'like':
      if not base_url:
        return source.creation_result(
          abort=True,
          error_plain='Could not find an Instagram post to like.',
          error_html='Could not find an Instagram post to <a href="http://indiewebcamp.com/like">like</a>. '
          'Check that your post has a like-of link to an Instagram URL or to an original post that publishes a '
          '<a href="http://indiewebcamp.com/rel-syndication">rel-syndication</a> link to Instagram.')

      if preview:
        return source.creation_result(
          description='<span class="verb">like</span> <a href="%s">'
                      'this post</a>:\n%s' % (base_url, self.embed_post(base_obj)))

      if not base_id:
        shortcode = self.post_id(base_url)
        logging.debug('looking up media by shortcode %s', shortcode)
        media_entry = self.urlopen(API_MEDIA_SHORTCODE_URL % shortcode) or {}
        base_id = media_entry.get('id')
        base_url = media_entry.get('link')

      logging.info('posting like for media id id=%s, url=%s',
                   base_id, base_url)
      # no response other than success/failure
      self.urlopen(API_MEDIA_LIKES_URL % base_id, data=urllib.parse.urlencode({
        'access_token': self.access_token
      }))
      # TODO use the stored user_json rather than looking it up each time.
      # oauth-dropins auth_entities should have the user_json.
      me = self.urlopen(API_USER_URL % 'self')
      return source.creation_result(
        self.like_to_object(me, base_id, base_url))

    return source.creation_result(
      abort=True,
      error_plain='Cannot publish this post on Instagram. Instagram does not support posting photos or videos from 3rd party applications.',
      error_html='Cannot publish this post on Instagram. Instagram <a href="http://instagram.com/developer/endpoints/media/#get_media_popular">does not support</a> posting photos or videos from 3rd party applications.')

  def media_to_activity(self, media):
    """Converts a media to an activity.

    http://instagram.com/developer/endpoints/media/#get_media

    Args:
      media: JSON object retrieved from the Instagram API

    Returns:
      an ActivityStreams activity dict, ready to be JSON-encoded
    """
    # Instagram timestamps are evidently all PST.
    # http://stackoverflow.com/questions/10320607
    object = self.media_to_object(media)
    activity = {
      'verb': 'post',
      'published': object.get('published'),
      'id': object['id'],
      'url': object.get('url'),
      'actor': object.get('author'),
      'object': object,
    }

    return self.postprocess_activity(activity)

  def media_to_object(self, media):
    """Converts a media to an object.

    Args:
      media: JSON object retrieved from the Instagram API

    Returns:
      an ActivityStreams object dict, ready to be JSON-encoded
    """
    id = media.get('id')
    user = media.get('user', {})
    content = xml.sax.saxutils.escape(media.get('caption', {}).get('text', ''))
    object = {
      'id': self.tag_uri(id),
      'ig_shortcode': media.get('code') or media.get('shortcode'),
      # TODO: detect videos. (the type field is in the JSON respose but not
      # propagated into the Media object.)
      'objectType': OBJECT_TYPES.get(media.get('type', 'image'), 'photo'),
      'published': util.maybe_timestamp_to_rfc3339(media.get('created_time')),
      'author': self.user_to_actor(user),
      'content': content,
      'url': media.get('link'),
      'to': [{
        'objectType': 'group',
        'alias': '@private' if user.get('is_private') else '@public',
      }],
      'attachments': [{
        'objectType': 'video' if 'videos' in media else 'image',
        'url': media.get('link'),
        # ActivityStreams 2.0 allows image to be a JSON array.
        # http://jasnell.github.io/w3c-socialwg-activitystreams/activitystreams2.html#link
        'image': sorted(
          media.get('images', {}).values(),
          # sort by size, descending, since atom.py
          # uses the first image in the list.
          key=operator.itemgetter('width'), reverse=True),
        # video object defined in
        # http://activitystrea.ms/head/activity-schema.html#rfc.section.4.18
        'stream': sorted(
          media.get('videos', {}).values(),
          key=operator.itemgetter('width'), reverse=True),
      }],
      # comments go in the replies field, according to the "Responses for
      # Activity Streams" extension spec:
      # http://activitystrea.ms/specs/json/replies/1.0/
      'replies': {
        'items': [self.comment_to_object(c, id, media.get('link'))
                  for c in media.get('comments', {}).get('data', [])],
        'totalItems': media.get('comments', {}).get('count'),
      },
      'tags': [{
        'objectType': 'hashtag',
        'id': self.tag_uri(tag),
        'displayName': tag,
        # TODO: url
      } for tag in media.get('tags', [])] +
      [self.user_to_actor(u.get('user'))
       for u in media.get('users_in_photo', [])] +
      [self.like_to_object(u, id, media.get('link'))
       for u in media.get('likes', {}).get('data', [])] +

      self._mention_tags_from_content(content)
    }

    # alt text
    # https://instagram-press.com/blog/2018/11/28/creating-a-more-accessible-instagram/
    alt = media.get('accessibility_caption')
    if alt and not any(alt.startswith(prefix) for prefix in AUTO_ALT_TEXT_PREFIXES):
      for att in object['attachments']:
        for img in att.get('image', []):
          img['displayName'] = alt

    for version in ('standard_resolution', 'low_resolution', 'thumbnail'):
      image = media.get('images', {}).get(version)
      if image:
        object['image'] = {'url': image.get('url')}
        break

    for version in ('standard_resolution', 'low_resolution', 'low_bandwidth'):
      video = media.get('videos', {}).get(version)
      if video:
        object['stream'] = {'url': video.get('url')}
        break

    # http://instagram.com/developer/endpoints/locations/
    if 'location' in media:
      media_loc = media.get('location', {})
      object['location'] = {
        'id': self.tag_uri(media_loc.get('id')),
        'displayName': media_loc.get('name'),
        'latitude': media_loc.get('point', {}).get('latitude'),
        'longitude': media_loc.get('point', {}).get('longitude'),
        'address': {'formatted': media_loc.get('street_address')},
        'url': (media_loc.get('id')
                and 'https://instagram.com/explore/locations/%s/'
                % media_loc.get('id')),
      }

    return self.postprocess_object(object)

  def _mention_tags_from_content(self, content):
    return [{
      'objectType': 'person',
      'id': self.tag_uri(mention.group(1)),
      'displayName': mention.group(1),
      'url': self.user_url(mention.group(1)),
      'startIndex': mention.start(),
      'length': mention.end() - mention.start(),
    } for mention in MENTION_RE.finditer(content)]

  def comment_to_object(self, comment, media_id, media_url):
    """Converts a comment to an object.

    Args:
      comment: JSON object retrieved from the Instagram API
      media_id: string
      media_url: string

    Returns:
      an ActivityStreams object dict, ready to be JSON-encoded
    """
    content = comment.get('text') or ''
    return self.postprocess_object({
      'objectType': 'comment',
      'id': self.tag_uri(comment.get('id')),
      'inReplyTo': [{'id': self.tag_uri(media_id)}],
      'url': '%s#comment-%s' % (media_url, comment.get('id')) if media_url else None,
      # TODO: add PST time zone
      'published': util.maybe_timestamp_to_rfc3339(comment.get('created_time')),
      'content': content,
      'author': self.user_to_actor(comment.get('from')),
      'to': [{'objectType': 'group', 'alias': '@public'}],
      'tags': self._mention_tags_from_content(content),
    })

  def like_to_object(self, liker, media_id, media_url):
    """Converts a like to an object.

    Args:
      liker: JSON object from the Instagram API, the user who does the liking
      media_id: string
      media_url: string

    Returns:
      an ActivityStreams object dict, ready to be JSON-encoded
    """
    return self.postprocess_object({
        'id': self.tag_uri('%s_liked_by_%s' % (media_id, liker.get('id'))),
        'url': '%s#liked-by-%s' % (media_url, liker.get('id')) if media_url else None,
        'objectType': 'activity',
        'verb': 'like',
        'object': {'url': media_url},
        'author': self.user_to_actor(liker),
    })

  def user_to_actor(self, user):
    """Converts a user to an actor.

    Args:
      user: JSON object from the Instagram API

    Returns:
      an ActivityStreams actor dict, ready to be JSON-encoded
    """
    if not user:
      return {}

    id = user.get('id')
    username = user.get('username')
    actor = {
      'id': self.tag_uri(id or username),
      'username': username,
      'objectType': 'person',
    }
    if not id or not username:
      return actor

    urls = [self.user_url(username)] + sum(
      (util.extract_links(user.get(field)) for field in ('website', 'bio')), [])
    actor.update({
      'url': urls[0],
      'urls': [{'value': u} for u in urls] if len(urls) > 1 else None
    })

    private = user.get('is_private')
    if private is not None:
      actor['to'] = [{
        'objectType': 'group',
        'alias': '@private' if private else '@public',
      }]

    pic_url = user.get('profile_picture') or user.get('profile_pic_url') or ''

    actor.update({
      'displayName': user.get('full_name') or username,
      'image': {'url': pic_url.replace(r'\/', '/')},
      'description': user.get('bio')
    })

    return util.trim_nulls(actor)

  def base_object(self, obj):
    """Extends the default base_object() to avoid using shortcodes as object ids.
    """
    base_obj = super(Instagram, self).base_object(obj)

    base_id = base_obj.get('id')
    if base_id and not base_id.replace('_', '').isdigit():
      # this isn't id. it's probably a shortcode.
      del base_obj['id']
      id = obj.get('id')
      if id:
        parsed = util.parse_tag_uri(id)
        if parsed and '_' in parsed[1]:
          base_obj['id'] = parsed[1].split('_')[0]

    return base_obj

  @staticmethod
  def id_to_shortcode(id):
    """Converts a media id to the shortcode used in its instagram.com URL.

    Based on http://carrot.is/coding/instagram-ids , which determined that
    shortcodes are just URL-safe base64 encoded ids.
    """
    if not id:
      return None

    if isinstance(id, str):
      parts = id.split('_')
      if not util.is_int(parts[0]):
        return id
      id = int(parts[0])

    chars = []
    while id > 0:
      id, rem = divmod(id, 64)
      chars.append(BASE64[rem])

    return ''.join(reversed(chars))

  def scraped_to_activities(self, html, cookie=None, count=None,
                            fetch_extras=False):
    """Converts scraped Instagram HTML to ActivityStreams activities.

    The input HTML may be from:

    * a user's feed, eg https://www.instagram.com/ while logged in
    * a user's profile, eg https://www.instagram.com/snarfed/
    * a photo or video, eg https://www.instagram.com/p/BBWCSrfFZAk/

    Args:
      html: unicode string
      cookie: string, optional sessionid cookie to be used for subsequent HTTP
        fetches, if necessary.
      count: integer, number of activities to return, None for all
      fetch_extras: whether to make extra HTTP fetches to get likes, etc.

    Returns:
      tuple, ([ActivityStreams activities], ActivityStreams viewer actor)
    """
    cookie = cookie or self.cookie
    # extract JSON data blob
    # (can also get just this JSON by adding ?__a=1 to any IG URL.)
    matches = HTML_DATA_RE.findall(html)
    if not matches:
      # Instagram sometimes returns 200 with incomplete HTML. often it stops at
      # the end of one of the <style> tags inside <head>. not sure why.
      logging.warning('JSON script tag not found!')
      return [], None

    # find media
    medias = []
    profile_user = None
    viewer_user = None

    for match in matches:
      data = util.trim_nulls(json_loads(match[1]))
      entry_data = data.get('entry_data', {})

      # home page ie news feed
      for page in entry_data.get('FeedPage', []):
        edges = page.get('graphql', {}).get('user', {})\
                    .get('edge_web_feed_timeline', {}).get('edges', [])
        medias.extend(e.get('node') for e in edges
                      if e.get('node', {}).get('__typename') not in
                      ('GraphSuggestedUserFeedUnit',))

      if 'user' in data:
        edges = data['user'].get('edge_web_feed_timeline', {}).get('edges', [])
        medias.extend(e.get('node') for e in edges)

      # user profiles
      for page in entry_data.get('ProfilePage', []):
        profile_user = page.get('graphql', {}).get('user', {})
        medias.extend(edge['node'] for edge in
          profile_user.get('edge_owner_to_timeline_media', {}).get('edges', [])
          if edge.get('node'))

      if not viewer_user:
        viewer_user = data.get('config', {}).get('viewer')

      # individual photo/video permalinks
      for page in [data] + entry_data.get('PostPage', []):
        media = page.get('graphql', {}).get('shortcode_media')
        if media:
          medias.append(media)

    if not medias:
      # As of 2018-02-15, embedded JSON in logged in https://www.instagram.com/
      # no longer has any useful data. Need to do a second header link fetch.
      soup = util.parse_html(html)
      link = soup.find('link', href=HTML_PRELOAD_RE)
      if link:
        url = urllib.parse.urljoin(HTML_BASE_URL, link['href'])
        data = self._scrape_json(url, cookie=cookie)
        edges = data.get('data', {}).get('user', {})\
                    .get('edge_web_feed_timeline', {}).get('edges', [])
        medias = [e.get('node') for e in edges]

    if count:
      medias = medias[:count]

    activities = []
    for media in util.trim_nulls(medias):
      activity = self._json_media_node_to_activity(media, user=profile_user)

      # likes
      shortcode = media.get('code') or media.get('shortcode')
      likes = media.get('edge_media_preview_like') or {}
      if shortcode and fetch_extras and likes.get('count') and not likes.get('edges'):
        # extra GraphQL fetch to get likes, as of 8/2018
        likes_json = self._scrape_json(HTML_LIKES_URL % shortcode, cookie=cookie)
        self.merge_scraped_reactions(likes_json, activity)

      activities.append(util.trim_nulls(activity))

    user = self._json_user_to_user(viewer_user or profile_user)
    actor = self.user_to_actor(user) if user else None
    return activities, actor

  html_to_activities = scraped_to_activities

  def scraped_to_activity(self, html, **kwargs):
    """Converts HTML from photo/video permalink page to an AS1 activity.

    Args:
      html: string, HTML from a photo/video page on instagram.com
      kwargs: passed through to scraped_to_activities

    Returns:
      tuple: (AS activity or None, AS logged in actor (ie viewer))
    """
    activities, actor = self.scraped_to_activities(html, **kwargs)
    return (activities[0] if activities else None), actor

  def scraped_to_actor(self, html, **kwargs):
    """Extracts and returns the logged in actor from any Instagram HTML.

    Args:
      html: unicode string

    Returns: dict, AS1 actor
    """
    return self.scraped_to_activities(html, **kwargs)[1]

  def merge_scraped_reactions(self, scraped, activity):
    """Converts and merges scraped likes and reactions into an activity.

    New likes and emoji reactions are added to the activity in 'tags'.
    Existing likes and emoji reactions in 'tags' are ignored.

    Args:
      scraped: str or dict, scraped JSON likes
      activity: dict, AS activity to merge these reactions into

    Returns:
      list of dict AS like tag objects converted from scraped

    Raises:
      ValueError: if scraped is not valid JSON
    """
    if isinstance(scraped, str):
      scraped = json_loads(scraped)

    media = scraped.get('data', {}).get('shortcode_media', {})
    if media:
      id = util.parse_tag_uri(activity['id'])[1]
      media_url = self.media_url(media['shortcode'])
      likes = [self.like_to_object(like.get('node', {}), id, media_url)
               for like in media.get('edge_liked_by', {}).get('edges', [])]
      source.merge_by_id(activity['object'], 'tags', likes)
      return likes

    return []

  @staticmethod
  def _scrape_json(url, cookie=None):
    """Fetches and returns JSON from www.instagram.com."""
    headers = {}
    if cookie:
      if not cookie.startswith('sessionid='):
        cookie = 'sessionid=' + cookie
      headers = {'Cookie': cookie, 'User-Agent': HTML_USER_AGENT}

    resp = util.requests_get(url, allow_redirects=False, headers=headers)
    resp.raise_for_status()

    try:
      return json_loads(resp.text)
    except ValueError:
      msg = "Couldn't decode response as JSON:\n%s" % resp.text
      logging.error(msg, exc_info=True)
      resp.status_code = 504
      raise requests.HTTPError('504 Bad response from Instagram\n' + msg,
                               response=resp)

  def _json_media_node_to_activity(self, media, user=None):
    """Converts Instagram HTML JSON media node to ActivityStreams activity.

    Args:
      media: dict, subset of Instagram HTML JSON representing a single photo
        or video
      user: top-level user object from Instagram HTML JSON, e.g. on a profile page

    Returns:
      dict, ActivityStreams activity
    """
    # preprocess to make its field names match the API's
    owner = media.get('owner', {})
    owner_id = owner.get('id')
    if user and user.get('id') == owner_id:
      owner.update(user)

    dims = media.get('dimensions', {})
    image_url = media.get('display_src') or media.get('display_url') or ''
    link = self.media_url(media.get('code') or media.get('shortcode'))
    media.update({
      'link': link,
      'user': self._json_user_to_user(owner),
      'created_time': media.get('date') or media.get('taken_at_timestamp'),
      'caption': {'text': media.get('edge_media_to_caption', {})
                               .get('edges', [{}])[0].get('node', {}).get('text', '')},
      'images': {'standard_resolution': {
        'url': image_url.replace(r'\/', '/'),
        'width': dims.get('width'),
        'height': dims.get('height'),
      }},
      'users_in_photo': (media.get('usertags', {}).get('nodes', []) +
                         [e.get('node', {}) for e in
                          media.get('edge_media_to_tagged_user', {}).get('edges', [])]),
    })

    id = media.get('id')
    owner_id = owner.get('id')
    if id and owner_id:
      media['id'] = '%s_%s' % (id, owner_id)

    comments_edge = (media.get('comments') or media.get('edge_media_to_comment') or
                     media.get('edge_media_to_parent_comment') or {})
    comments = [c.get('node') for c in comments_edge.get('edges', [])]
    count = comments_edge.get('count')
    for comment in comments:
      threaded = comment.get('edge_threaded_comments')
      if threaded:
        comments += [c.get('node') for c in threaded.get('edges', [])]
        threaded_count = threaded.get('count')
        if threaded_count:
          count = count + threaded_count if count else threaded_count

    media['comments'] = {
      'data': comments,
      'count': count,
    }

    likes = media.get('likes') or media.get('edge_media_preview_like') or {}
    media['likes'] = {
      'data': [l.get('node') for l in likes.get('edges', [])],
      'count': likes.get('count'),
    }

    for obj in [media] + media['comments']['data'] + media['likes']['data']:
      obj.setdefault('user', obj.get('owner') or {})
      user = obj['user'] or obj
      if not user.get('profile_picture'):
        user['profile_picture'] = user.get('profile_pic_url', '').replace(r'\/', '/')

    for c in media['comments']['data']:
      c['from'] = c['user']
      c['created_time'] = c['created_at']

    if media.get('is_video'):
      media.update({
        'type': 'video',
        'videos': {'standard_resolution': {
          'url': media.get('video_url', '').replace(r'\/', '/'),
          'width': dims.get('width'),
          'height': dims.get('height'),
        }},
      })

    activity = self.media_to_activity(util.trim_nulls(media))
    obj = activity['object']
    obj['ig_like_count'] = media['likes'].get('count', 0)

    # multi-photo
    children = media.get('edge_sidecar_to_children', {}).get('edges', [])
    if children:
      obj['attachments'] = []
      for child in children:
        child_activity = self._json_media_node_to_activity(child.get('node'))
        for att in child_activity['object']['attachments']:
          if not att.get('url'):
            att['url'] = link
          obj['attachments'].append(att)

    self.postprocess_object(obj)
    return super(Instagram, self).postprocess_activity(activity)

  def _json_user_to_user(self, user):
    """Converts an Instagram HTML JSON user to an API actor.

    Args:
      media: dict, HTML JSON user

    Returns:
      dict, API user object
    """
    if not user:
      return None

    if user.get('user'):
      user = user['user']
    profile = user.get('profile_pic_url')
    if profile:
      user['profile_picture'] = profile.replace(r'\/', '/')
    website = user.get('external_url')
    if website:
      user['website'] = website.replace(r'\/', '/')
    user.setdefault('bio', user.get('biography'))

    return user
