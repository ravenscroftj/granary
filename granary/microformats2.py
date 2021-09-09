"""Convert ActivityStreams to microformats2 HTML and JSON.

Microformats2 specs: http://microformats.org/wiki/microformats2
ActivityStreams 1 specs: http://activitystrea.ms/specs/
"""
from collections import defaultdict
import copy
import html
import itertools
import logging
import urllib.parse
import string
import re
import xml.sax.saxutils

import humanfriendly
import mf2util
from oauth_dropins.webutil import util
from oauth_dropins.webutil.util import (
  dedupe_urls,
  get_first,
  get_list,
  get_url,
  get_urls,
  uniquify,
)

from . import source

HENTRY = string.Template("""\
<article class="$types">
  <span class="p-uid">$uid</span>
  $summary
  $published
  $updated
$author
  $linked_name
  <div class="$content_classes">
  $invitees
  $content
  </div>
$attachments
$sizes
$event_times
$location
$categories
$links
$children
$comments
</article>
""")
HCARD = string.Template("""\
  <span class="$types">
    $ids
    $linked_name
    $nicknames
    $photos
  </span>
""")
LINK = string.Template('  <a class="u-$cls" href="$url"></a>')
AS_TO_MF2_TYPE = {
  'event': ['h-event'],
  'person': ['h-card'],
  'place': ['h-card', 'p-location'],
}
MF2_TO_AS_TYPE_VERB = {
  'article': ('article', None),
  'bookmark': ('activity', 'post'),
  'event': ('event', None),
  'follow': ('activity', 'follow'),
  'invite': ('activity', 'invite'),
  'like': ('activity', 'like'),
  'location': ('place', None),
  'note': ('note', None),
  'person': ('person', None),
  'reply': ('comment', None),
  'repost': ('activity', 'share'),
  'rsvp': ('activity', None),  # json_to_object() will generate verb from rsvp
  'tag': ('activity', 'tag'),
}
# ISO 6709 location string. http://en.wikipedia.org/wiki/ISO_6709
ISO_6709_RE = re.compile(r'^([-+][0-9.]+)([-+][0-9.]+).*/$')


def get_string_urls(objs):
  """Extracts string URLs from a list of either string URLs or mf2 dicts.

  Many mf2 properties can contain either string URLs or full mf2 objects, e.g.
  h-cites. in-reply-to is the most commonly used example:
  http://indiewebcamp.com/in-reply-to#How_to_consume_in-reply-to

  Args:
    objs: sequence of either string URLs or embedded mf2 objects

  Returns:
    list of string URLs
  """
  if not objs:
    return []

  urls = []
  for item in objs:
    if isinstance(item, str):
      urls.append(item)
    else:
      itemtype = [x for x in item.get('type', []) if x.startswith('h-')]
      if itemtype:
        item = item.get('properties') or item
        urls.extend(get_string_urls(item.get('url', [])))

  return urls


def get_html(val):
  """Returns a string value that may have HTML markup.

  Args:
    value: mf2 property value, either string or
     {'html': '<p>str</p>', 'value': 'str'} dict

  Returns:
    string or None
  """
  if isinstance(val, dict) and val.get('html'):
    return val['html'].strip()

  return html.escape(get_text(val), quote=False)


def get_text(val):
  """Returns a plain text string value. See get_html."""
  if isinstance(val, dict):
    val = val.get('value')
  return val.strip() if val else ''


def activity_to_json(activity, **kwargs):
  """Converts an ActivityStreams activity to microformats2 JSON.

  Args:
    activity: dict, a decoded JSON ActivityStreams activity
    kwargs: passed to object_to_json

  Returns:
    dict, decoded microformats2 JSON
  """
  return object_to_json(_activity_or_object(activity), **kwargs)


def _activity_or_object(activity):
  """Returns the base item we care about, activity or activity['object'].

  Used in :func:`activity_to_json()` and :func:`activities_to_html()`.
  """
  if activity.get('object') and activity.get('verb') not in source.VERBS_WITH_OBJECT:
    return activity['object']

  return activity


def object_to_json(obj, trim_nulls=True, entry_class='h-entry',
                   default_object_type=None, synthesize_content=True):
  """Converts an ActivityStreams object to microformats2 JSON.

  Args:
    obj: dict, a decoded JSON ActivityStreams object
    trim_nulls: boolean, whether to remove elements with null or empty values
    entry_class: string or sequence, the mf2 class(es) that entries should be
      given (e.g. 'h-cite' when parsing a reference to a foreign entry).
      defaults to 'h-entry'
    default_object_type: string, the ActivityStreams objectType to use if one
      is not present. defaults to None
    synthesize_content: whether to generate synthetic content if the object
      doesn't have its own, e.g. 'likes this.' or 'shared this.'

  Returns:
    dict, decoded microformats2 JSON
  """
  if not obj or not isinstance(obj, dict):
    return {}

  obj_type = source.object_type(obj) or default_object_type
  # if the activity type is a post, then it's really just a conduit
  # for the object. for other verbs, the activity itself is the
  # interesting thing
  if obj_type == 'post':
    primary = obj.get('object', {})
    obj_type = source.object_type(primary) or default_object_type
  else:
    primary = obj

  # TODO: extract snippet
  name = primary.get('displayName', primary.get('title'))
  summary = primary.get('summary')
  author = obj.get('author', obj.get('actor', {}))

  in_reply_tos = obj.get('inReplyTo') or []
  if not in_reply_tos:
    context = obj.get('context')
    if context and isinstance(context, dict):
      in_reply_tos = context.get('inReplyTo') or []

  is_rsvp = obj_type in ('rsvp-yes', 'rsvp-no', 'rsvp-maybe')
  if (is_rsvp or obj_type == 'react') and obj.get('object'):
    objs = obj['object']
    in_reply_tos.extend(objs if isinstance(objs, list) else [objs])

  # maps objectType to list of objects
  attachments = defaultdict(list)
  for prop in 'attachments', 'tags':
    for elem in get_list(primary, prop):
      attachments[elem.get('objectType')].append(elem)

  # prefer duration and size from object's stream, then first video, then first
  # audio
  stream = {}
  for candidate in [obj] + attachments['video'] + attachments['audio']:
    for stream in get_list(candidate, 'stream'):
      if stream:
        break

  duration = stream.get('duration')
  if duration is not None:
    if util.is_int(duration):
      duration = str(duration)
    else:
      logging('Ignoring duration %r; expected int, got %s', duration.__class__)
      duration = None

  size = stream.get('size')
  sizes = [str(size)] if size else []
  # construct mf2!
  ret = {
    'type': (AS_TO_MF2_TYPE.get(obj_type) or
             [entry_class] if isinstance(entry_class, str)
             else list(entry_class)),
    'properties': {
      'uid': [obj.get('id') or ''],
      'numeric-id': [obj.get('numeric_id') or ''],
      'name': [name],
      'nickname': [obj.get('username') or ''],
      'summary': [summary],
      'url': (list(object_urls(obj) or object_urls(primary)) +
              obj.get('upstreamDuplicates', [])),
      # photo is special cased below, to handle alt
      'video': dedupe_urls(get_urls(attachments, 'video', 'stream') +
                           get_urls(primary, 'stream')),
      'audio': get_urls(attachments, 'audio', 'stream'),
      'duration': [duration],
      'size': sizes,
      'published': [obj.get('published', primary.get('published', ''))],
      'updated': [obj.get('updated', primary.get('updated', ''))],
      'in-reply-to': util.trim_nulls([o.get('url') for o in in_reply_tos]),
      'author': [object_to_json(
        author, trim_nulls=False, default_object_type='person')],
      'location': [object_to_json(
        primary.get('location', {}), trim_nulls=False,
        default_object_type='place')],
      'comment': [object_to_json(c, trim_nulls=False, entry_class='h-cite')
                  for c in obj.get('replies', {}).get('items', [])],
      'start': [primary.get('startTime')],
      'end': [primary.get('endTime')],
    },
    'children': (
      # silly hack: i haven't found anywhere in AS1 or AS2 to indicate that
      # something is being "quoted," like in a quote tweet, so i cheat and use
      # extra knowledge here that quoted tweets are converted to note
      # attachments, but URLs in the tweet text are converted to article tags.
      [object_to_json(a, trim_nulls=False, entry_class=['u-quotation-of', 'h-cite'])
       for a in attachments['note'] if 'startIndex' not in a] +
      [object_to_json(a, trim_nulls=False, entry_class=['h-cite'])
       for a in attachments['article'] if 'startIndex' not in a])
  }

  # content. emulate e- vs p- microformats2 parsing: e- if there are HTML tags,
  # otherwise p-.
  # https://indiewebcamp.com/note#Indieweb_whitespace_thinking
  text = xml.sax.saxutils.unescape(primary.get('content') or '')
  html = render_content(primary, include_location=False,
                        synthesize_content=synthesize_content)
  if '<' in html:
    ret['properties']['content'] = [{'value': text, 'html': html}]
  else:
    ret['properties']['content'] = [text]

  # photos, including alt text
  photo_urls = set()
  ret['properties']['photo'] = []
  for image in get_list(attachments, 'image') + [primary]:
    for url in get_urls(image, 'image'):
      if url and url not in photo_urls:
        photo_urls.add(url)
        name = get_first(image, 'image', {}).get('displayName')
        ret['properties']['photo'].append({'value': url, 'alt': name} if name else url)

  # hashtags and person tags
  if obj_type == 'tag':
    ret['properties']['tag-of'] = util.get_urls(obj, 'target')

  tags = obj.get('tags', []) or get_first(obj, 'object', {}).get('tags', [])
  if not tags and obj_type == 'tag':
    tags = util.get_list(obj, 'object')
  ret['properties']['category'] = []
  for tag in tags:
    if tag.get('objectType') == 'person':
      ret['properties']['category'].append(
        object_to_json(tag, entry_class='u-category h-card'))
    elif tag.get('objectType') == 'hashtag' or obj_type == 'tag':
      name = tag.get('displayName')
      if name:
        ret['properties']['category'].append(name)

  # rsvp
  if is_rsvp:
    ret['properties']['rsvp'] = [obj_type[len('rsvp-'):]]
  elif obj_type == 'invite':
    invitee = object_to_json(obj.get('object'), trim_nulls=False,
                             default_object_type='person')
    ret['properties']['invitee'] = [invitee]

  # like and repost mentions
  for type, prop in (
      ('favorite', 'like'),
      ('follow', 'follow'),
      ('like', 'like'),
      ('share', 'repost'),
  ):
    if obj_type == type:
      # The ActivityStreams spec says the object property should always be a
      # single object, but it's useful to let it be a list, e.g. when a like has
      # multiple targets, e.g. a like of a post with original post URLs in it,
      # which brid.gy does.
      objs = get_list(obj, 'object')
      ret['properties'][prop + '-of'] = [
        # flatten contexts that are just a url
        o['url'] if 'url' in o and set(o.keys()) <= set(['url', 'objectType'])
        else object_to_json(o, trim_nulls=False, entry_class='h-cite')
        for o in objs]
    else:
      # received likes and reposts
      ret['properties'][prop] = [
        object_to_json(t, trim_nulls=False, entry_class='h-cite')
        for t in tags if source.object_type(t) == type]

  # bookmarks
  if obj_type == 'bookmark':
    ret['properties']['bookmark-of'] = [primary.get('targetUrl')]

  # latitude & longitude
  lat = long = None
  position = ISO_6709_RE.match(primary.get('position') or '')
  if position:
    lat, long = position.groups()
  if not lat:
    lat = primary.get('latitude')
  if not long:
    long = primary.get('longitude')

  if lat:
    ret['properties']['latitude'] = [str(lat)]
  if long:
    ret['properties']['longitude'] = [str(long)]

  if trim_nulls:
    ret = util.trim_nulls(ret)
  return ret


def json_to_object(mf2, actor=None, fetch_mf2=False):
  """Converts a single microformats2 JSON item to an ActivityStreams object.

  Supports h-entry, h-event, h-card, and other single item times. Does *not* yet
  support h-feed.

  Args:
    mf2: dict, decoded JSON microformats2 object
    actor: optional author AS actor object. usually comes from a rel="author"
      link. if mf2 has its own author, that will override this.
    fetch_mf2: boolean, whether to fetch additional pages via HTTP if necessary,
      e.g. to determine authorship: https://indieweb.org/authorship

  Returns:
    dict, ActivityStreams object
  """
  if not mf2 or not isinstance(mf2, dict):
    return {}

  mf2 = copy.copy(mf2)
  props = mf2.setdefault('properties', {})
  prop = first_props(props)
  rsvp = prop.get('rsvp')

  # convert author
  mf2_author = prop.get('author')
  if mf2_author and isinstance(mf2_author, dict):
    author = json_to_object(mf2_author)
  else:
    # the author h-card may be on another page. run full authorship algorithm:
    # https://indieweb.org/authorship
    author = mf2util.find_author({'items': [mf2]}, hentry=mf2,
                                 fetch_mf2_func=util.fetch_mf2 if fetch_mf2 else None)
    if author:
      author = {
        'objectType': 'person',
        'url': author.get('url'),
        'displayName': author.get('name'),
        'image': [{'url': author.get('photo')}],
      }

  if not author:
    author = actor

  mf2_types = mf2.get('type') or []
  if 'h-geo' in mf2_types or 'p-location' in mf2_types:
    mf2_type = 'location'
  elif 'tag-of' in props:
    # TODO: remove once this is in mf2util
    # https://github.com/kylewm/mf2util/issues/18
    mf2_type = 'tag'
  elif 'follow-of' in props:  # ditto
    mf2_type = 'follow'
  elif 'bookmark-of' in props:  # ditto
    mf2_type = 'bookmark'
  else:
    # mf2 'photo' type is a note or article *with* a photo, but AS 'photo' type
    # *is* a photo. so, special case photo type to fall through to underlying
    # mf2 type without photo.
    # https://github.com/snarfed/bridgy/issues/702
    without_photo = copy.deepcopy(mf2)
    without_photo.get('properties', {}).pop('photo', None)
    mf2_type = mf2util.post_type_discovery(without_photo)

  as_type, as_verb = MF2_TO_AS_TYPE_VERB.get(mf2_type, (None, None))
  if rsvp:
    as_verb = 'rsvp-%s' % rsvp

  # special case GitHub issues that are in-reply-to the repo or its issues URL
  in_reply_tos = get_string_urls(props.get('in-reply-to', []))
  for url in in_reply_tos:
    if re.match(r'^https?://github.com/[^/]+/[^/]+(/issues)?/?$', url):
      as_type = 'issue'

  def is_absolute(url):
    """Filter out relative and invalid URLs (mf2py gives absolute urls)."""
    return urllib.parse.urlparse(url).netloc

  urls = props.get('url') and get_string_urls(props.get('url'))

  # quotations: https://indieweb.org/quotation#How_to_markup
  attachments = [
    json_to_object(quote)
    for quote in mf2.get('children', []) + props.get('quotation-of', [])
    if isinstance(quote, dict) and 'h-cite' in set(quote.get('type', []))]

  # audio and video
  #
  # the duration mf2 property is still emerging. examples in the wild use both
  # integer seconds and ISO 8601 durations.
  # https://indieweb.org/duration
  # https://en.wikipedia.org/wiki/ISO_8601#Durations
  duration = prop.get('duration') or prop.get('length')
  if duration:
    if util.is_int(duration):
      duration = int(duration)
    else:
      parsed = util.parse_iso8601_duration(duration)
      if parsed:
        duration = int(parsed.total_seconds())
      else:
        logging.debug('Unknown format for length or duration %r', duration)
        duration = None

  stream = None
  bytes = size_to_bytes(prop.get('size'))
  for type in 'audio', 'video':
    atts = [{
      'objectType': type,
      'stream': {
        'url': url,
        # integer seconds: http://activitystrea.ms/specs/json/1.0/#media-link
        'duration': duration,
        # file size in bytes. nonstandard, not in AS1 or AS2
        'size': bytes,
      },
    } for url in get_string_urls(props.get(type, []))]
    attachments.extend(atts)
    if atts:
      stream = atts[0]['stream']

  obj = {
    'id': prop.get('uid'),
    'objectType': as_type,
    'verb': as_verb,
    'published': prop.get('published', ''),
    'updated': prop.get('updated', ''),
    'startTime': prop.get('start'),
    'endTime': prop.get('end'),
    'displayName': get_text(prop.get('name')),
    'username': prop.get('nickname'),
    'summary': get_text(prop.get('summary')),
    'content': get_html(prop.get('content')),
    'url': urls[0] if urls else None,
    'urls': [{'value': u} for u in urls] if urls and len(urls) > 1 else None,
    # image is special cased below, to handle alt
    'stream': [stream],
    'location': json_to_object(prop.get('location')),
    'replies': {'items': [json_to_object(c) for c in props.get('comment', [])]},
    'tags': [{'objectType': 'hashtag', 'displayName': cat}
             if isinstance(cat, str)
             else json_to_object(cat)
             for cat in props.get('category', [])],
    'attachments': attachments,
  }

  # images, including alt text
  photo_urls = set()
  obj['image'] = []
  for photo in props.get('photo', []) + props.get('featured', []):
    url = photo
    alt = None
    if isinstance(photo, dict):
      photo = photo.get('properties') or photo
      url = get_first(photo, 'value') or get_first(photo, 'url')
      alt = get_first(photo, 'alt')
    if url and url not in photo_urls and is_absolute(url):
      photo_urls.add(url)
      obj['image'].append({'url': url, 'displayName': alt})

  # mf2util uses the indieweb/mf2 location algorithm to collect location properties.
  interpreted = mf2util.interpret({'items': [mf2]}, None)
  if interpreted:
    loc = interpreted.get('location')
    if loc:
      obj['location']['objectType'] = 'place'
      lat, lng = loc.get('latitude'), loc.get('longitude')
      if lat and lng:
        try:
          obj['location'].update({
            'latitude': float(lat),
            'longitude': float(lng),
          })
        except ValueError:
          logging.debug(
            'Could not convert latitude/longitude (%s, %s) to decimal', lat, lng)

  if as_type == 'activity':
    objects = []
    for target in itertools.chain.from_iterable(
        props.get(field, []) for field in (
          'follow-of', 'like', 'like-of', 'repost', 'repost-of', 'in-reply-to',
          'invitee')):
      t = json_to_object(target) if isinstance(target, dict) else {'url': target}
      # eliminate duplicates from redundant backcompat properties
      if t not in objects:
        objects.append(t)

    objects.extend({'objectType': 'bookmark', 'targetUrl': url}
                   for url in get_string_urls(props.get('bookmark-of', [])))

    obj.update({
      'object': objects[0] if len(objects) == 1 else objects,
      'actor': author,
    })
    if as_verb == 'tag':
      obj['target'] = {'url': prop['tag-of']}
      if obj.get('object'):
        raise NotImplementedError(
          'Combined in-reply-to and tag-of is not yet supported.')
      obj['object'] = obj.pop('tags')

  else:
    obj.update({
      'inReplyTo': [{'url': url} for url in in_reply_tos],
      'author': author,
    })

  return source.Source.postprocess_object(obj)


def html_to_activities(html, url=None, actor=None, id=None):
  """Converts a microformats2 HTML h-feed to ActivityStreams activities.

  Args:
    html: unicode string HTML or :class:`requests.Response`
    url: optional string URL that HTML came from
    actor: optional author AS actor object for all activities. usually comes
      from a rel="author" link.
    id: string, optional id of specific element to extract and parse. defaults
      to the whole page.

  Returns:
    list of ActivityStreams activity dicts
  """
  parsed = util.parse_mf2(html, url=url, id=id)
  hfeed = mf2util.find_first_entry(parsed, ['h-feed'])
  items = hfeed.get('children', []) if hfeed else parsed.get('items', [])

  activities = []
  for item in items:
    types = item.get('type', [])
    if 'h-entry' in types or 'h-event' in types or 'h-cite' in types:
      obj = json_to_object(item, actor=actor)
      obj['content_is_html'] = True
      activities.append({'object': obj})

  return activities


def activities_to_html(activities):
  """Converts ActivityStreams activities to a microformats2 HTML h-feed.

  Args:
    obj: dict, a decoded JSON ActivityStreams object

  Returns:
    string, the content field in obj with the tags in the tags field
    converted to links if they have startIndex and length, otherwise added to
    the end.
  """
  return """\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body>
%s
</body>
</html>
""" % '\n'.join(object_to_html(_activity_or_object(a)) for a in activities)


def object_to_html(obj, parent_props=None, synthesize_content=True):
  """Converts an ActivityStreams object to microformats2 HTML.

  Features:

  * linkifies embedded tags and adds links for other tags
  * linkifies embedded URLs
  * adds links, summaries, and thumbnails for attachments and checkins
  * adds a "via SOURCE" postscript

  Args:
    obj: dict, a decoded JSON ActivityStreams object
    parent_props: list of strings, the properties of the parent object where
      this object is embedded, e.g. ['u-repost-of']
    synthesize_content: whether to generate synthetic content if the object
      doesn't have its own, e.g. 'likes this.' or 'shared this.'

  Returns:
    string, the content field in obj with the tags in the tags field
    converted to links if they have startIndex and length, otherwise added to
    the end.
  """
  return json_to_html(object_to_json(obj, synthesize_content=synthesize_content),
                      parent_props=parent_props)


def json_to_html(obj, parent_props=None):
  """Converts a microformats2 JSON object to microformats2 HTML.

  See object_to_html for details.

  Args:
    obj: dict, a decoded microformats2 JSON object
    parent_props: list of strings, the properties of the parent object where
      this object is embedded, e.g. 'u-repost-of'

  Returns:
    string HTML
  """

  if not obj:
    return ''
  if not parent_props:
    parent_props = []

  types = obj.get('type', [])
  if 'h-card' in types:
    return hcard_to_html(obj, parent_props)

  props = copy.copy(obj.get('properties', {}))

  links = []
  for prop in 'in-reply-to', 'tag-of':
    links.extend(LINK.substitute(cls=prop, url=url)
                 for url in sorted(get_string_urls(props.get(prop, []))))

  prop = first_props(props)
  prop.setdefault('uid', '')
  author = prop.get('author')

  # if this post is an rsvp, populate its data element. if it's an invite, give
  # it a default name.
  # do this *before* content since it sets props['name'] if necessary.
  rsvp = prop.get('rsvp')
  if rsvp:
    if not props.get('name'):
      props['name'] = [{'yes': 'is attending.',
                        'no': 'is not attending.',
                        'maybe': 'might attend.'}.get(rsvp)]
    props['name'][0] = '<data class="p-rsvp" value="%s">%s</data>' % (
      rsvp, props['name'][0])

  elif props.get('invitee') and not props.get('name'):
    props['name'] = ['invited']

  children = []

  # if this post is itself a follow, like, or repost, link to its target(s).
  for mftype in ['follow', 'like', 'repost']:
    for target in props.get(mftype + '-of', []):
      if isinstance(target, str):
        children.append('<a class="u-%s-of" href="%s"></a>' % (mftype, target))
      else:
        children.append(json_to_html(target, ['u-' + mftype + '-of']))

  # set up content and name
  content_html = get_html(prop.get('content', {}))
  content_classes = []

  if content_html:
    content_classes.append('e-content')
    if not props.get('name'):
      content_classes.append('p-name')
  else:
    # if content is empty, set explicit blank name to prevent bad (old)
    # microformats2 implied p-name handling.
    # https://github.com/snarfed/granary/issues/131
    if not props.get('name'):
      props['name'] = ['']

  summary = ('<div class="p-summary">%s</div>' % prop.get('summary')
             if prop.get('summary') else '')

  # attachments
  # TODO: use photo alt property as alt text once mf2py handles that.
  # https://github.com/tommorris/mf2py/issues/83
  attachments = []
  for name, fn in ('photo', img), ('video', vid), ('audio', aud):
    attachments.extend(fn(val) for val in props.get(name, []))

  # size(s)
  # https://github.com/snarfed/granary/issues/169#issuecomment-547918405
  sizes = []
  for size in props.get('size', []):
    bytes = size_to_bytes(size)
    if size:
      sizes.append('<data class="p-size" value="%s">%s</data>' %
                   (bytes, humanfriendly.format_size(bytes)))

  # categories
  cats = props.get('category', [])
  people = [
    hcard_to_html(cat, ['u-category', 'h-card']) for cat in cats
    if isinstance(cat, dict) and 'h-card' in cat.get('type')
    and not cat.get('startIndex')]  # mentions are already linkified in content
  tags = ['<span class="u-category">%s</span>' % cat
          for cat in cats if isinstance(cat, str)]

  # comments
  # http://indiewebcamp.com/comment-presentation#How_to_markup
  # http://indiewebcamp.com/h-cite
  comments_html = '\n'.join(json_to_html(c, ['p-comment'])
                            for c in props.get('comment', []))

  # embedded likes and reposts of this post
  # http://indiewebcamp.com/like, http://indiewebcamp.com/repost
  for verb in 'like', 'repost':
    # including u-like and u-repost for backcompat means that we must ignore
    # these properties when converting a post that is itself a like or repost
    if verb + '-of' not in props:
      vals = props.get(verb, [])
      if vals and isinstance(vals[0], dict):
        children += [json_to_html(v, ['u-' + verb]) for v in vals]

  # embedded children of this post
  children += [json_to_html(c) for c in obj.get('children', [])]

  # location; make sure it's an object
  location = prop.get('location')
  if isinstance(location, str):
    location = {'properties': {'name': [location]}}

  # event times
  event_times = []
  start = props.get('start', [])
  end = props.get('end', [])
  event_times += ['  <time class="dt-start">%s</time>' % time for time in start]
  if start and end:
    event_times.append('  to')
  event_times += ['  <time class="dt-end">%s</time>' % time for time in end]

  return HENTRY.substitute(
    prop,
    published=maybe_datetime(prop.get('published'), 'dt-published'),
    updated=maybe_datetime(prop.get('updated'), 'dt-updated'),
    types=' '.join(parent_props + types),
    author=hcard_to_html(author, ['p-author']),
    location=hcard_to_html(location, ['p-location']),
    categories='\n'.join(people + tags),
    attachments='\n'.join(attachments),
    sizes='\n'.join(sizes),
    links='\n'.join(links),
    invitees='\n'.join([hcard_to_html(i, ['p-invitee'])
                        for i in props.get('invitee', [])]),
    content=content_html,
    content_classes=' '.join(content_classes),
    comments=comments_html,
    children='\n'.join(children),
    linked_name=maybe_linked_name(props),
    summary=summary,
    event_times='\n'.join(event_times))


def hcard_to_html(hcard, parent_props=None):
  """Renders an h-card as HTML.

  Args:
    hcard: dict, decoded JSON h-card
    parent_props: list of strings, the properties of the parent object where
      this object is embedded, e.g. ['p-author']

  Returns:
    string, rendered HTML
  """
  if not hcard:
    return ''
  if not parent_props:
    parent_props = []

  # extract first value from multiply valued properties
  props = hcard.get('properties', {})
  prop = first_props(props)
  if not prop:
    return ''

  return HCARD.substitute(
    types=' '.join(uniquify(parent_props + hcard.get('type', []))),
    ids='\n'.join(['<data class="p-uid" value="%s"></data>' % uid
                   for uid in props.get('uid', []) if uid] +
                  ['<data class="p-numeric-id" value="%s"></data>' % nid
                   for nid in props.get('numeric-id', []) if nid]),
    linked_name=maybe_linked_name(props),
    nicknames='\n'.join('<span class="p-nickname">%s</span>' % nick
                        for nick in props.get('nickname', []) if nick),
    photos='\n'.join(img(photo) for photo in props.get('photo', []) if photo),
  )


def render_content(obj, include_location=True, synthesize_content=True,
                   render_attachments=False, render_image=False,
                   white_space_pre=True):
  """Renders the content of an ActivityStreams object as HTML.

  Includes tags, mentions, and non-note/article attachments. (Note/article
  attachments are converted to mf2 children in object_to_json and then rendered
  in json_to_html.)

  Note that the returned HTML is included in Atom as well as HTML documents,
  so it *must* be HTML4 / XHTML, not HTML5! All tags must be closed, etc.

  Args:
    obj: decoded JSON ActivityStreams object
    include_location: boolean, whether to render location, if provided
    synthesize_content: boolean, whether to generate synthetic content if the
      object doesn't have its own, e.g. 'likes this.' or 'shared this.'
    render_attachments: boolean, whether to render attachments, eg links,
      images, audio, and video
    render_image: boolean, whether to render the object's image(s)
    white_space_pre: boolean, whether to wrap in CSS white-space: pre. If False,
      newlines will be converted to <br> tags instead. Background:
      https://indiewebcamp.com/note#Indieweb_whitespace_thinking

  Returns:
    string, rendered HTML
  """
  obj_type = source.object_type(obj)
  content = obj.get('content') or ''

  # extract tags. preserve order but de-dupe, ie don't include a tag more than
  # once.
  seen_ids = set()
  mentions = []
  tags = {}  # maps string objectType to list of tag objects
  for t in obj.get('tags', []):
    id = t.get('id')
    if id and id in seen_ids:
      continue
    seen_ids.add(id)

    if 'startIndex' in t and 'length' in t and 'url' in t:
      mentions.append(t)
    else:
      tags.setdefault(source.object_type(t), []).append(t)

  # linkify embedded mention tags inside content.
  if mentions:
    mentions.sort(key=lambda t: t['startIndex'])
    last_end = 0
    orig = util.WideUnicode(content)
    content = util.WideUnicode('')
    for tag in mentions:
      start = tag['startIndex']
      end = start + tag['length']
      content = util.WideUnicode('%s%s<a href="%s">%s</a>' % (
        content, orig[last_end:start], tag['url'], orig[start:end]))
      last_end = end

    content += orig[last_end:]

  # is whitespace in this content meaningful? standard heuristic: if there are
  # no HTML tags in it, and it has a newline, then assume yes.
  # https://indiewebcamp.com/note#Indieweb_whitespace_thinking
  # https://github.com/snarfed/granary/issues/80
  if content and not obj.get('content_is_html') and '\n' in content:
    if white_space_pre:
      content = '<div style="white-space: pre">%s</div>' % content
    else:
      content = content.replace('\n', '<br />\n')

  # linkify embedded links. ignore the "mention" tags that we added ourselves.
  # TODO: fix the bug in test_linkify_broken() in webutil/tests/test_util.py, then
  # uncomment this.
  # if content:
  #   content = util.linkify(content)

  # the image field. may be multiply valued.
  rendered_urls = set()
  if render_image:
    urls = get_urls(obj, 'image')
    content += _render_attachments([{
      'objectType': 'image',
      'image': {'url': url},
    } for url in urls], obj)
    rendered_urls = set(urls)

  # bookmarked URL
  targetUrl = obj.get('targetUrl')
  if obj_type == 'bookmark' and targetUrl:
    content += '\nBookmark: %s' % util.pretty_link(
      targetUrl, attrs={'class': 'u-bookmark-of'})

  # attachments, e.g. links (aka articles)
  # TODO: use oEmbed? http://oembed.com/ , http://code.google.com/p/python-oembed/
  if render_attachments:
    atts = [a for a in obj.get('attachments', [])
            if a.get('objectType') not in ('note', 'article')
            and get_url(a, 'image') not in rendered_urls]
    content += _render_attachments(atts + tags.pop('article', []), obj)

  # generate share/like contexts if the activity does not have content
  # of its own
  for as_type, verb in (
      ('favorite', 'Favorites'), ('like', 'Likes'), ('share', 'Shared')):
    if (not synthesize_content or obj_type != as_type or 'object' not in obj or
        'content' in obj):
      continue

    targets = get_list(obj, 'object')
    if not targets:
      continue

    for target in targets:
      # sometimes likes don't have enough content to render anything
      # interesting
      if 'url' in target and set(target) <= set(['url', 'objectType']):
        content += '<a href="%s">%s this.</a>' % (
          target.get('url'), verb.lower())

      else:
        author = target.get('author', target.get('actor', {}))
        # special case for twitter RT's
        if obj_type == 'share' and 'url' in obj and re.search(
            r'^https?://(?:www\.|mobile\.)?twitter\.com/', obj.get('url')):
          content += 'RT <a href="%s">@%s</a> ' % (
            target.get('url', '#'), author.get('username'))
        else:
          # image looks bad in the simplified rendering
          author = {k: v for k, v in author.items() if k != 'image'}
          content += '%s <a href="%s">%s</a> by %s' % (
            verb, target.get('url', '#'),
            target.get('displayName', target.get('title', 'a post')),
            hcard_to_html(object_to_json(author, default_object_type='person')),
          )
        content += render_content(target, include_location=include_location,
                                  synthesize_content=synthesize_content,
                                  white_space_pre=white_space_pre)
      # only include the first context in the content (if there are
      # others, they'll be included as separate properties)
      break
    break

  if render_attachments and obj.get('verb') == 'share':
    atts = [att for att in itertools.chain.from_iterable(
              o.get('attachments', []) for o in util.get_list(obj, 'object'))
            if att.get('objectType') not in ('note', 'article')]
    content += _render_attachments(atts, obj)

  # location
  loc = obj.get('location')
  if include_location and loc:
    content += '\n<p>%s</p>' % hcard_to_html(
      object_to_json(loc, default_object_type='place'),
      parent_props=['p-location'])

  # these are rendered manually in json_to_html()
  for type in 'like', 'share', 'react', 'person':
    tags.pop(type, None)

  # render the rest
  content += tags_to_html(tags.pop('hashtag', []), 'p-category')
  content += tags_to_html(tags.pop('mention', []), 'u-mention', visible=False)
  content += tags_to_html(sum(tags.values(), []), 'tag')

  return content


def _render_attachments(attachments, obj):
  """Renders ActivityStreams attachments (or tags etc) as HTML.

  Note that the returned HTML is included in Atom as well as HTML documents,
  so it *must* be HTML4 / XHTML, not HTML5! All tags must be closed, etc.

  Args:
    attachments: sequence of decoded JSON ActivityStreams objects
    obj: top-level decoded JSON ActivityStreams object

  Returns:
    string, rendered HTML
  """
  content = ''

  for att in attachments:
    name = att.get('displayName') or ''
    stream = get_first(att, 'stream', {}).get('url') or ''
    image = get_first(att, 'image', {}).get('url') or ''
    open_a_tag = False
    content += '\n<p>'

    type = att.get('objectType')
    if type == 'video':
      if stream:
        content += vid(stream, poster=image)
    elif type == 'audio':
      if stream:
        content += aud(stream)
    else:
      url = att.get('url') or obj.get('url')
      if url:
        content += '\n<a class="link" href="%s">' % url
        open_a_tag = True
      if image:
        content += '\n' + img(image, name)

    if name and type != 'image':
      content += '\n<span class="name">%s</span>' % name

    if open_a_tag:
      content += '\n</a>'

    summary = att.get('summary')
    if summary and summary != name:
      content += '\n<span class="summary">%s</span>' % summary
    content += '\n</p>'

  return content


def find_author(parsed, **kwargs):
  """Returns the author of a page as a ActivityStreams actor dict.

  Args:
    parsed: dict, parsed mf2 object (ie return value from mf2py.parse())
    kwargs: passed through to mf2util.find_author()
  """
  author = mf2util.find_author(parsed, 'http://123', **kwargs)
  if author:
    photo = author.get('photo')
    if isinstance(photo, dict):
      photo = photo.get('url') or photo.get('value')
    return {
      'displayName': author.get('name'),
      'url': author.get('url'),
      'image': {'url': photo},
    }


def get_title(mf2):
  """Returns an mf2 object's title, ie its name.

  Args:
    mf2: dict, parsed mf2 object (ie return value from mf2py.parse())

  Returns: string title, possibly ellipsized
  """
  lines = mf2util.interpret_feed(mf2, '').get('name', '').splitlines()
  if lines:
    return util.ellipsize(lines[0])

  return ''


def first_props(props):
  """Converts a multiply-valued dict to singly valued.

  Args:
    props: dict of properties, where each value is a sequence

  Returns:
    corresponding dict with just the first value of each sequence, or ''
    if the sequence is empty
  """
  return {k: get_first(props, k, '') for k in props} if props else {}


def tags_to_html(tags, classname, visible=True):
  """Returns an HTML string with links to the given tag objects.

  Args:
    tags: decoded JSON ActivityStreams objects.
    classname: class for span to enclose tags in
    visible: boolean, whether to visibly include displayName
  """
  urls = {}  # stores (url, displayName) tuples
  for tag in tags:
    name = ''
    if visible and tag.get('displayName'):
      name = tag['displayName']
    # loop through individually instead of using update() so that order is
    # preserved.
    for url in object_urls(tag):
      urls[url, name] = None

  return ''.join('\n<a class="%s" %shref="%s">%s</a>' %
                 (classname, '' if name else 'aria-hidden="true" ', url, name)
                 for url, name in sorted(urls.keys()))


def object_urls(obj):
  """Returns an object's unique URLs, preserving order.
  """
  if isinstance(obj, str):
    return obj
  return uniquify(util.trim_nulls(
    [obj.get('url')] + [u.get('value') for u in obj.get('urls', [])]))


def author_display_name(hcard):
  """Returns a human-readable string display name for an h-card object."""
  name = None
  if hcard:
    prop = first_props(hcard.get('properties'))
    name = prop.get('name') or prop.get('uid')
  return name if name else 'Unknown'


def maybe_linked_name(props):
  """Returns the HTML for a p-name with an optional u-url inside.

  Args:
    props: *multiply-valued* properties dict

  Returns:
    string HTML
  """
  prop = first_props(props)
  name = prop.get('name')
  url = prop.get('url')

  if name is not None:
    html = maybe_linked(name, url, linked_classname='p-name u-url',
                        unlinked_classname='p-name')
  else:
    html = maybe_linked(url or '', url, linked_classname='u-url')

  extra_urls = props.get('url', [])[1:]
  if extra_urls:
    html += '\n' + '\n'.join(maybe_linked('', url, linked_classname='u-url')
                             for url in extra_urls)

  return html


def img(src, alt=''):
  """Returns an <img> string with the given src, class, and alt.

  Args:
    src: string url or dict with value and (optionally) alt
    alt: string, alt attribute value, or None

  Returns:
    string
  """
  if isinstance(src, dict):
    assert not alt
    alt = src.get('alt') or ''
    src = src.get('value')
  return '<img class="u-photo" src="%s" alt=%s />' % (
      src, xml.sax.saxutils.quoteattr(alt or ''))


def vid(src, poster=''):
  """Returns an <video> string with the given src and class

  Args:
    src: string, url of the video
    poster: sring, optional. url of the poster or preview image

  Returns:
    string
  """
  poster_img = '<img src="%s" />' % poster if poster else ''

  # include ="controls" value since this HTML is also used in the Atom
  # template, which has to validate as XML.
  return '<video class="u-video" src="%s" controls="controls" poster="%s">Your browser does not support the video tag. <a href="%s">Click here to view directly. %s</a></video>' % (
    src, poster, src, poster_img)


def aud(src):
  """Returns an <audio> string with the given src and class

  Args:
    src: string, url of the audio

  Returns:
    string
  """
  return '<audio class="u-audio" src="%s" controls="controls">Your browser does not support the audio tag. <a href="%s">Click here to listen directly.</a></audio>' % (src, src)


def maybe_linked(text, url, linked_classname=None, unlinked_classname=None):
  """Wraps text in an <a href=...> iff a non-empty url is provided.

  Args:
    text: string
    url: string or None
    linked_classname: string, optional class attribute to use if url
    unlinked_classname: string, optional class attribute to use if not url

  Returns:
    string
  """
  if url:
    classname = ' class="%s"' % linked_classname if linked_classname else ''
    return '<a%s href="%s">%s</a>' % (classname, url, text)
  if unlinked_classname:
    return '<span class="%s">%s</span>' % (unlinked_classname, text)
  return text


def maybe_datetime(str, classname):
  """Returns a <time datetime=...> elem if str is non-empty.

  Args:
    str: string RFC339 datetime or None
    classname: string class name

  Returns:
    string
  """
  if str:
    return '<time class="%s" datetime="%s">%s</time>' % (classname, str, str)
  else:
    return ''


def size_to_bytes(size):
  """Converts a string file size to an integer number of bytes.

  Args:
    size, string, may be either integer bytes or human-readable approximation,
      eg 7MB or 1.23 kb

  Returns: integer, bytes, or None if size can't be parsed
  """
  if util.is_int(size):
    return int(size)

  if not size:
    return None

  try:
    return humanfriendly.parse_size(size)
  except humanfriendly.InvalidSize:
    logging.debug("Couldn't parse size %r", size)


def prefix_image_urls(activity, prefix):
  """Adds a prefix to all image url fields, eg to inject a caching proxy.

  Skips any image url fields that already start with the prefix. URLs are *not*
  URL-encode before adding the prefix. (This is currently used with
  https://cloudimage.io/ , which doesn't URL-decode.)

  Args:
    activity: dict, AS1 activity. Modified in place.
    prefix: string
  """
  a = activity
  for elem in ([a, a.get('object'), a.get('author'), a.get('actor')] +
               a.get('replies', {}).get('items', []) +
               a.get('attachments', []) +
               a.get('tags', [])):
    if elem:
      for img in util.get_list(elem, 'image'):
        url = img.get('url')
        if url and not url.startswith(prefix):
          # Note that url isn't URL-encoded here, that's intentional.
          # cloudimage.io doesn't decode it.
          img['url'] = prefix + url
      if elem is not a:
        prefix_image_urls(elem, prefix)


# TODO: unify
def prefix_video_urls(activity, prefix):
  a = activity
  for elem in ([a, a.get('object'), a.get('author'), a.get('actor')] +
               a.get('replies', {}).get('items', []) +
               a.get('attachments', []) +
               a.get('tags', [])):
    if elem:
      for stream in util.get_list(elem, 'stream'):
        url = stream.get('url')
        if url and not url.startswith(prefix):
          # Note that url isn't URL-encoded here, that's intentional.
          # The caching-proxy Cloudflare worker doesn't decode it.
          stream['url'] = prefix + url
      if elem is not a:
        prefix_video_urls(elem, prefix)
