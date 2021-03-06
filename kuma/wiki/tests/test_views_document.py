"""
Tests for kuma/wiki/views/document.py

Legacy tests are in test_views.py.
"""
import json
import base64
from collections import namedtuple

import mock
import pytest
import requests_mock
from pyquery import PyQuery as pq

from kuma.core.models import IPBan
from kuma.core.urlresolvers import reverse
from kuma.authkeys.models import Key
from kuma.wiki.models import Document, Revision
from kuma.wiki.views.utils import calculate_etag
from kuma.wiki.views.document import _apply_content_experiment

from django.utils.text import compress_string
from django.utils.six.moves.urllib.parse import urlparse
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart


AuthKey = namedtuple('AuthKey', 'key header')

SECTION1 = '<h3 id="S1">Section 1</h3><p>This is a page. Deal with it.</p>'
SECTION2 = '<h3 id="S2">Section 2</h3><p>This is a page. Deal with it.</p>'
SECTION3 = '<h3 id="S3">Section 3</h3><p>This is a page. Deal with it.</p>'
SECTION4 = '<h3 id="S4">Section 4</h3><p>This is a page. Deal with it.</p>'
SECTIONS = SECTION1 + SECTION2 + SECTION3 + SECTION4
SECTION_CASE_TO_DETAILS = {
    'no-section': (None, SECTIONS),
    'section': ('S1', SECTION1),
    'another-section': ('S3', SECTION3),
    'non-existent-section': ('S99', '')
}


def get_content(content_case, data):
    if content_case == 'multipart':
        return MULTIPART_CONTENT, encode_multipart(BOUNDARY, data)

    if content_case == 'json':
        return 'application/json', json.dumps(data)

    if content_case == 'html-fragment':
        return 'text/html', data['content']

    if content_case == 'html':
        return 'text/html', """
            <html>
                <head>
                    <title>%(title)s</title>
                </head>
                <body>%(content)s</body>
            </html>
        """ % data

    raise ValueError('unsupported content case')


@pytest.fixture
def section_doc(root_doc, wiki_user):
    """
    The content in this document's current revision contains multiple HTML
    elements with an "id" attribute (or "sections"), and also has a length
    greater than or equal to 200, which meets the compression threshold of
    the GZipMiddleware.
    """
    root_doc.current_revision = Revision.objects.create(
        document=root_doc, creator=wiki_user, content=SECTIONS)
    root_doc.save()
    return root_doc


@pytest.fixture
def ce_settings(settings):
    settings.CONTENT_EXPERIMENTS = [{
        'id': 'experiment-test',
        'ga_name': 'experiment-test',
        'param': 'v',
        'pages': {
            'en-US:Original': {
                'control': 'Original',
                'test': 'Experiment:Test/Variant',
            }
        }
    }]
    return settings


@pytest.fixture
def authkey(wiki_user):
    key = Key(user=wiki_user, description='Test Key 1')
    secret = key.generate_secret()
    key.save()
    auth = '%s:%s' % (key.key, secret)
    header = 'Basic %s' % base64.encodestring(auth)
    return AuthKey(key=key, header=header)


@pytest.mark.parametrize('method', ('GET', 'HEAD'))
@pytest.mark.parametrize('if_none_match', (None, 'match', 'mismatch'))
@pytest.mark.parametrize(
    'section_case',
    ('no-section', 'section', 'another-section', 'non-existent-section')
)
def test_api_safe(client, section_doc, section_case, if_none_match, method):
    """
    Test GET & HEAD on wiki.document_api endpoint.
    """
    section_id, exp_content = SECTION_CASE_TO_DETAILS[section_case]

    url = section_doc.get_absolute_url() + '$api'

    if section_id:
        url += '?section={}'.format(section_id)

    headers = dict(HTTP_ACCEPT_ENCODING='gzip')

    if if_none_match == 'match':
        response = getattr(client, method.lower())(url, **headers)
        assert 'etag' in response
        headers['HTTP_IF_NONE_MATCH'] = response['etag']
    elif if_none_match == 'mismatch':
        headers['HTTP_IF_NONE_MATCH'] = 'ABC'

    response = getattr(client, method.lower())(url, **headers)

    if if_none_match == 'match':
        exp_content = ''
        assert response.status_code == 304
    else:
        assert response.status_code == 200
        assert 'etag' in response
        assert 'x-kuma-revision' in response
        assert 'last-modified' not in response
        assert '"{}"'.format(calculate_etag(exp_content)) in response['etag']
        assert (response['x-kuma-revision'] ==
                str(section_doc.current_revision_id))

    if method == 'GET':
        if response.get('content-encoding') == 'gzip':
            exp_content = compress_string(exp_content)
        assert response.content == exp_content


@pytest.mark.parametrize('user_case', ('authenticated', 'anonymous'))
def test_api_put_forbidden_when_no_authkey(client, user_client, root_doc,
                                           user_case):
    """
    A PUT to the wiki.document_api endpoint should forbid access without
    an authkey, even for logged-in users.
    """
    url = root_doc.get_absolute_url() + '$api'
    response = (client if user_case == 'anonymous' else user_client).put(url)
    assert response.status_code == 403


def test_api_put_unsupported_content_type(client, authkey):
    """
    A PUT to the wiki.document_api endpoint with an unsupported content
    type should return a 400.
    """
    url = '/en-US/docs/foobar$api'
    response = client.put(
        url,
        data='stuff',
        content_type='nonsense',
        HTTP_AUTHORIZATION=authkey.header
    )
    assert response.status_code == 400


def test_api_put_authkey_tracking(client, authkey):
    """
    Revisions modified by PUT API should track the auth key used
    """
    url = '/en-US/docs/foobar$api'
    data = dict(
        title="Foobar, The Document",
        content='<p>Hello, I am foobar.</p>',
    )
    content_type, encoded_data = get_content('json', data)
    response = client.put(
        url,
        data=encoded_data,
        content_type=content_type,
        HTTP_AUTHORIZATION=authkey.header
    )
    assert response.status_code == 201
    last_log = authkey.key.history.order_by('-pk').all()[0]
    assert last_log.action == 'created'

    data['title'] = 'Foobar, The New Document'
    content_type, encoded_data = get_content('json', data)
    response = client.put(
        url,
        data=encoded_data,
        content_type=content_type,
        HTTP_AUTHORIZATION=authkey.header
    )
    assert response.status_code == 205
    last_log = authkey.key.history.order_by('-pk').all()[0]
    assert last_log.action == 'updated'


@pytest.mark.parametrize('if_match', (None, 'match', 'mismatch'))
@pytest.mark.parametrize(
    'content_case',
    ('multipart', 'json', 'html-fragment', 'html')
)
@pytest.mark.parametrize(
    'section_case',
    ('no-section', 'section', 'another-section', 'non-existent-section')
)
def test_api_put_existing(client, section_doc, authkey, section_case,
                          content_case, if_match):
    """
    A PUT to the wiki.document_api endpoint should allow the modification
    of an existing document's content.
    """
    orig_rev_id = section_doc.current_revision_id
    section_id, section_content = SECTION_CASE_TO_DETAILS[section_case]

    url = section_doc.get_absolute_url() + '$api'

    if section_id:
        url += '?section={}'.format(section_id)

    headers = dict(HTTP_AUTHORIZATION=authkey.header)

    if if_match == 'match':
        response = client.get(url, HTTP_ACCEPT_ENCODING='gzip')
        assert 'etag' in response
        headers['HTTP_IF_MATCH'] = response['etag']
    elif if_match == 'mismatch':
        headers['HTTP_IF_MATCH'] = 'ABC'

    data = dict(
        comment="I like this document.",
        title="New Sectioned Root Document",
        summary="An edited sectioned root document.",
        content="<p>This is an edit.</p>",
        tags="tagA,tagB,tagC",
        review_tags="editorial,technical",
    )

    content_type, encoded_data = get_content(content_case, data)

    response = client.put(
        url,
        data=encoded_data,
        content_type=content_type,
        **headers
    )

    if content_case == 'html-fragment':
        expected_title = section_doc.title
    else:
        expected_title = data['title']

    if section_content:
        expected_content = SECTIONS.replace(section_content, data['content'])
    else:
        expected_content = SECTIONS

    if if_match == 'mismatch':
        assert response.status_code == 412
    else:
        assert response.status_code == 205
        # Confirm that the PUT worked.
        section_doc.refresh_from_db()
        assert section_doc.current_revision_id != orig_rev_id
        assert section_doc.title == expected_title
        assert section_doc.html == expected_content
        if content_case in ('multipart', 'json'):
            rev = section_doc.current_revision
            assert rev.summary == data['summary']
            assert rev.comment == data['comment']
            assert rev.tags == data['tags']
            assert (set(rev.review_tags.names()) ==
                    set(data['review_tags'].split(',')))


@pytest.mark.parametrize('slug_case', ('root', 'child', 'nonexistent-parent'))
@pytest.mark.parametrize(
    'content_case',
    ('multipart', 'json', 'html-fragment', 'html')
)
@pytest.mark.parametrize('section_case', ('no-section', 'section'))
def test_api_put_new(settings, client, root_doc, authkey, section_case,
                     content_case, slug_case):
    """
    A PUT to the wiki.document_api endpoint should allow the creation
    of a new document and its initial revision.
    """
    locale = settings.WIKI_DEFAULT_LANGUAGE
    section_id, _ = SECTION_CASE_TO_DETAILS[section_case]

    if slug_case == 'root':
        slug = 'foobar'
    elif slug_case == 'child':
        slug = 'Root/foobar'
    else:
        slug = 'nonexistent/foobar'

    url_path = '/{}/docs/{}'.format(locale, slug)
    url = url_path + '$api'

    # The section_id should have no effect on the results, but we'll see.
    if section_id:
        url += '?section={}'.format(section_id)

    data = dict(
        comment="I like this document.",
        title="Foobar, The Document",
        summary="A sectioned document named foobar.",
        content=SECTIONS,
        tags="tagA,tagB,tagC",
        review_tags="editorial,technical",
    )

    content_type, encoded_data = get_content(content_case, data)

    response = client.put(
        url,
        data=encoded_data,
        content_type=content_type,
        HTTP_AUTHORIZATION=authkey.header,
    )

    if content_case == 'html-fragment':
        expected_title = slug
    else:
        expected_title = data['title']

    if slug_case == 'nonexistent-parent':
        assert response.status_code == 404
    else:
        assert response.status_code == 201
        assert 'location' in response
        assert urlparse(response['location']).path == url_path
        # Confirm that the PUT worked.
        doc = Document.objects.get(locale=locale, slug=slug)
        assert doc.title == expected_title
        assert doc.html == data['content']
        if content_case in ('multipart', 'json'):
            rev = doc.current_revision
            assert rev.summary == data['summary']
            assert rev.comment == data['comment']
            assert rev.tags == data['tags']
            assert (set(rev.review_tags.names()) ==
                    set(data['review_tags'].split(',')))


def test_conditional_get(client, section_doc):
    """
    Test conditional GET to document view (ETag only currently).
    """
    url = section_doc.get_absolute_url() + '$api'

    # Ensure the ETag value is based on the entire content of the response.
    response = client.get(url)
    assert response.status_code == 200
    assert 'etag' in response
    assert 'last-modified' not in response
    assert '"{}"'.format(calculate_etag(response.content)) in response['etag']

    # Get the ETag header value when using gzip to test that GZipMiddleware
    # plays nicely with ConditionalGetMiddleware when making the following
    # conditional request.
    response = client.get(url, HTTP_ACCEPT_ENCODING='gzip')
    assert response.status_code == 200
    assert 'etag' in response

    response = client.get(
        url,
        HTTP_ACCEPT_ENCODING='gzip',
        HTTP_IF_NONE_MATCH=response['etag']
    )

    assert response.status_code == 304


def test_apply_content_experiment_no_experiment(ce_settings, rf):
    """If not under a content experiment, use the original Document."""
    doc = mock.Mock(spec_set=['locale', 'slug'])
    doc.locale = 'en-US'
    doc.slug = 'Other'
    request = rf.get('/%s/docs/%s' % (doc.locale, doc.slug))

    experiment_doc, params = _apply_content_experiment(request, doc)

    assert experiment_doc == doc
    assert params is None


def test_apply_content_experiment_has_experiment(ce_settings, rf):
    """If under a content experiment, return original Document and params."""
    doc = mock.Mock(spec_set=['locale', 'slug'])
    doc.locale = 'en-US'
    doc.slug = 'Original'
    request = rf.get('/%s/docs/%s' % (doc.locale, doc.slug))

    experiment_doc, params = _apply_content_experiment(request, doc)

    assert experiment_doc == doc
    assert params == {
        'id': 'experiment-test',
        'ga_name': 'experiment-test',
        'param': 'v',
        'original_path': '/en-US/docs/Original',
        'variants': {
            'control': 'Original',
            'test': 'Experiment:Test/Variant',
        },
        'selected': None,
        'selection_is_valid': None,
    }


def test_apply_content_experiment_selected_original(ce_settings, rf):
    """If the original is selected as the content experiment, return it."""
    doc = mock.Mock(spec_set=['locale', 'slug'])
    db_doc = mock.Mock(spec_set=['locale', 'slug'])
    doc.locale = db_doc.locale = 'en-US'
    doc.slug = db_doc.slug = 'Original'
    request = rf.get('/%s/docs/%s' % (doc.locale, doc.slug), {'v': 'control'})

    with mock.patch(
            'kuma.wiki.views.document.Document.objects.get',
            return_value=db_doc) as mock_get:
        experiment_doc, params = _apply_content_experiment(request, doc)

    mock_get.assert_called_once_with(locale='en-US', slug='Original')
    assert experiment_doc == db_doc
    assert params['selected'] == 'control'
    assert params['selection_is_valid']


def test_apply_content_experiment_selected_variant(ce_settings, rf):
    """If the variant is selected as the content experiment, return it."""
    doc = mock.Mock(spec_set=['locale', 'slug'])
    db_doc = mock.Mock(spec_set=['locale', 'slug'])
    doc.locale = db_doc.locale = 'en-US'
    doc.slug = 'Original'
    db_doc.slug = 'Experiment:Test/Variant'
    request = rf.get('/%s/docs/%s' % (doc.locale, doc.slug), {'v': 'test'})

    with mock.patch(
            'kuma.wiki.views.document.Document.objects.get',
            return_value=db_doc) as mock_get:
        experiment_doc, params = _apply_content_experiment(request, doc)

    mock_get.assert_called_once_with(locale='en-US',
                                     slug='Experiment:Test/Variant')
    assert experiment_doc == db_doc
    assert params['selected'] == 'test'
    assert params['selection_is_valid']


def test_apply_content_experiment_bad_selection(ce_settings, rf):
    """If the variant is selected as the content experiment, return it."""
    doc = mock.Mock(spec_set=['locale', 'slug'])
    doc.locale = 'en-US'
    doc.slug = 'Original'
    request = rf.get('/%s/docs/%s' % (doc.locale, doc.slug), {'v': 'other'})

    experiment_doc, params = _apply_content_experiment(request, doc)

    assert experiment_doc == doc
    assert params['selected'] is None
    assert not params['selection_is_valid']


def test_apply_content_experiment_valid_selection_no_doc(ce_settings, rf):
    """If the Document for a variant doesn't exist, return the original."""
    doc = mock.Mock(spec_set=['locale', 'slug'])
    doc.locale = 'en-US'
    doc.slug = 'Original'
    request = rf.get('/%s/docs/%s' % (doc.locale, doc.slug), {'v': 'test'})

    with mock.patch(
            'kuma.wiki.views.document.Document.objects.get',
            side_effect=Document.DoesNotExist) as mock_get:
        experiment_doc, params = _apply_content_experiment(request, doc)

    mock_get.assert_called_once_with(locale='en-US',
                                     slug='Experiment:Test/Variant')
    assert experiment_doc == doc
    assert params['selected'] is None
    assert not params['selection_is_valid']


def test_document_banned_ip_can_read(client, root_doc):
    '''Banned IPs are still allowed to read content, just not edit.'''
    ip = '127.0.0.1'
    IPBan.objects.create(ip=ip)
    response = client.get(root_doc.get_absolute_url(), REMOTE_ADDR=ip)
    assert response.status_code == 200


@pytest.mark.parametrize('endpoint', ['document', 'preview'])
def test_kumascript_error_reporting(admin_client, root_doc, ks_toolbox,
                                    endpoint):
    """
    Kumascript reports errors in HTTP headers. Kuma should display the errors
    with appropriate links for both the "wiki.preview" and "wiki.document"
    endpoints.
    """
    ks_settings = dict(
        KUMASCRIPT_TIMEOUT=1.0,
        KUMASCRIPT_MAX_AGE=600,
        KUMA_DOCUMENT_FORCE_DEFERRED_TIMEOUT=10.0,
        KUMA_DOCUMENT_RENDER_TIMEOUT=180.0
    )
    mock_requests = requests_mock.Mocker()
    mock_ks_config = mock.patch('kuma.wiki.kumascript.config', **ks_settings)
    with mock_ks_config, mock_requests:
        if endpoint == 'preview':
            mock_requests.post(
                requests_mock.ANY,
                text='HELLO WORLD',
                headers=ks_toolbox.errors_as_headers,
            )
            mock_requests.get(
                requests_mock.ANY,
                **ks_toolbox.macros_response
            )
            response = admin_client.post(
                reverse('wiki.preview', locale=root_doc.locale),
                dict(content='anything truthy')
            )
        else:
            mock_requests.get(
                requests_mock.ANY,
                [
                    dict(
                        text='HELLO WORLD',
                        headers=ks_toolbox.errors_as_headers
                    ),
                    ks_toolbox.macros_response,
                ]
            )
            with mock.patch('kuma.wiki.models.config', **ks_settings):
                response = admin_client.get(root_doc.get_absolute_url())

    assert response.status_code == 200

    response_html = pq(response.content)
    macro_link = ('#kserrors-list a[href="https://github.com/'
                  'mdn/kumascript/blob/master/macros/{}.ejs"]')
    create_link = ('#kserrors-list a[href="https://github.com/'
                   'mdn/kumascript#updating-macros"]')
    assert len(response_html.find(macro_link.format('SomeMacro'))) == 1
    assert len(response_html.find(create_link)) == 1

    assert mock_requests.request_history[0].headers['X-FireLogger'] == '1.2'
    for error in ks_toolbox.errors['logs']:
        assert error['message'] in response.content


@pytest.mark.tags
def test_tags_show_in_document(root_doc, client, wiki_user):
    """Test tags are showing correctly in document view"""
    tags = ('JavaScript', 'AJAX', 'DOM')
    Revision.objects.create(document=root_doc, tags=','.join(tags), creator=wiki_user)
    response = client.get(root_doc.get_absolute_url())
    assert response.status_code == 200

    page = pq(response.content)
    response_tags = page.find('.tags li a').contents()
    assert len(response_tags) == len(tags)
    # The response tags should be sorted
    assert response_tags == sorted(tags)


@pytest.mark.tags
def test_tags_not_show_while_empty(root_doc, client, wiki_user):
    # Create a revision with no tags
    Revision.objects.create(document=root_doc, tags=','.join([]), creator=wiki_user)

    response = client.get(root_doc.get_absolute_url())
    assert response.status_code == 200

    page = pq(response.content)
    response_tags = page.find('.tags li a').contents()
    # There should be no tag
    assert len(response_tags) == 0
