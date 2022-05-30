#!/usr/bin/env python
# -*- coding: utf-8 -*-
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Flask Blueprint for our "API" module.

This is used to make API calls to GitHub, either via a logged-in users
credentials or as a proxy on behalf of anonymous or unauthenticated users.
"""

import json

from flask import abort
from flask import Blueprint
from flask import g
from flask import make_response
from flask import render_template
from flask import request
from flask import session

from webcompat import app
from webcompat.api.helpers import get_html_comments
from webcompat.helpers import api_request
from webcompat.helpers import get_comment_data
from webcompat.helpers import get_response_headers
from webcompat.helpers import mockable_response
from webcompat.helpers import normalize_api_params
from webcompat import limiter

api_bp = Blueprint('api_bp', __name__, url_prefix='/api',
                   template_folder='../templates')
JSON_MIME_HTML = 'application/vnd.github.v3.html+json'
HTML_MIME = 'text/html'
ISSUES_PATH = app.config['ISSUES_REPO_URI']
PRIVATE_ISSUES_PATH = app.config['PRIVATE_REPO_URI']
AUTOCLOSED_MILESTONE_ID = app.config['AUTOCLOSED_MILESTONE_ID']
REPO_PATH = ISSUES_PATH[:-7]


@api_bp.route('/issues/<int:number>')
def proxy_issue(number):
    """XHR endpoint to get issue data from GitHub.

    either as an authed user, or as one of our proxy bots.
    """
    path = 'repos/{0}/{1}'.format(ISSUES_PATH, number)
    return api_request('get', path, mime_type=JSON_MIME_HTML)


@api_bp.route('/issues/<int:number>/edit', methods=['PATCH'])
def edit_issue(number):
    """XHR endpoint to push back edits to GitHub for a single issue.

    - It only allows change of state and change of milestones.
    - It's not proxied, so only users with write access are
      able to edit issues.
      Format: {'milestone': 2, 'state': 'open'}
    """
    if not g.user:
        abort(403)

    path = 'repos/{0}/{1}'.format(ISSUES_PATH, number)
    patch_data = json.loads(request.data)
    # Create a list of associated milestones id with their mandatory state.
    STATUSES = app.config['STATUSES']
    valid_statuses = [(STATUSES[status]['id'], STATUSES[status]['state'])
                      for status in STATUSES]
    data_check = (patch_data['milestone'], patch_data['state'])
    # The PATCH data can only be of length: 2
    if data_check in valid_statuses and len(patch_data) == 2:
        (content, status_code, headers) = api_request('patch',
                                                      path, data=request.data)
        return (content, status_code,
                {'content-type': JSON_MIME_HTML})
    # Default will be 403 for this route
    abort(403)


@api_bp.route('/issues')
def proxy_issues():
    """List all issues from GitHub on the API endpoint."""
    params = request.args.copy()

    # If there's a q param, then we need to use the Search API
    # and load those results. For logged in users, we handle this at the
    # server level.
    if g.user and params.get('q'):
        return get_search_results(params.get('q'), params)
    # Non-authed users should never get here--the request is made to
    # GitHub client-side)--but return out of paranoia anyways.
    elif params.get('q'):
        abort(404)
    path = 'repos/{0}'.format(ISSUES_PATH)
    return api_request('get', path, params=params)


@api_bp.route('/private')
def proxy_autoclosed():
    """List all issues from GitHub on the API endpoint."""
    params = request.args.copy()
    params.add('milestone', AUTOCLOSED_MILESTONE_ID)

    if not g.user:
        abort(404)

    path = 'repos/{0}'.format(PRIVATE_ISSUES_PATH)
    return api_request('get', path, params=params)


@api_bp.route('/issues/<username>/<parameter>')
def get_user_activity_issues(username, parameter):
    """Return issues related to a user at the API endpoint.

    cf. https://developer.github.com/v3/issues/#list-issues-for-a-repository
    This is used for "creator" and "mentioned". A special "needsinfo" parameter
    value is converted into a request for labels of the format:

    `status-needsinfo-username`

    Any logged in user can see details for any other logged in user. We can
    extend this to non-logged in users in the future if we want.
    """
    if not g.user:
        abort(401)
    # copy the params so we can add to the dict.
    params = request.args.copy()
    params['state'] = 'all'
    if parameter == 'needsinfo':
        params['labels'] = 'status-needsinfo-{0}'.format(username)
    else:
        params[parameter] = username
    path = 'repos/{path}'.format(path=ISSUES_PATH)
    return api_request('get', path, params=params)


@api_bp.route('/issues/category/<issue_category>')
def get_issue_category(issue_category):
    """Return all issues for a specific category."""
    category_list = app.config['OPEN_STATUSES']
    issues_path = 'repos/{0}'.format(ISSUES_PATH)
    params = request.args.copy()
    if issue_category in category_list:
        STATUSES = app.config['STATUSES']
        params.add('milestone', STATUSES[issue_category]['id'])
        return api_request('get', issues_path, params=params)
    elif issue_category == 'closed':
        params['state'] = 'closed'
        return api_request('get', issues_path, params=params)
    else:
        # The path doesn’t exist. 404 Not Found.
        abort(404)


@api_bp.route('/issues/search')
@limiter.limit('30/minute',
               key_func=lambda: session.get('username', 'proxy-user'))
def get_search_results(query_string=None, params=None):
    """XHR endpoint to get results from GitHub's Search API.

    We're specifically searching "issues" here, which seems to make the most
    sense. Note that the rate limit is different for Search: 30 requests per
    minute.

    If a user hits the rate limit, the Flask Limiter extension will send a
    429. See @app.error_handler(429) in views.py.

    This method can take a query_string argument, to be called from other
    endpoints, or the query_string can be passed in via the Request object.
    """
    params = params or request.args.copy()
    query_string = query_string or params.get('q')
    # Fail early if no appropriate query_string
    if not query_string:
        abort(404)

    # restrict results to our repo.
    query_string += " repo:{0}".format(REPO_PATH)
    params['q'] = query_string

    # add a required parameter to request only issues and not PRs
    params['q'] += ' is:issue'

    # convert issues api to search api params here.
    params = normalize_api_params(params)
    path = 'search/issues'
    return api_request('get', path, params=params,
                       mime_type=JSON_MIME_HTML)


@api_bp.route('/issues/<int:number>/comments', methods=['GET', 'POST'])
def proxy_comments(number):
    """XHR endpoint for GitHub issue comments.

    * GET an issue comments
    * POST a comment on an issue (only as an authorized GitHub user)
    """
    params = request.args.copy()
    path = 'repos/{0}/{1}/comments'.format(ISSUES_PATH, number)
    if request.method == 'POST' and g.user:
        new_comment = api_request('post', path, params=params,
                                  data=get_comment_data(request.data),
                                  mime_type=JSON_MIME_HTML)
        return get_html_comments(new_comment)
    else:
        # TODO: handle the (rare) case for more than 1 page of comments
        # for now, we just get the first 100 and rely on the client to
        # fetch more
        params.update({'per_page': 100})
        comments_data = api_request('get', path, params=params,
                                    mime_type=JSON_MIME_HTML)
        comments_status = comments_data[1:2]
        if comments_status != 304:
            return get_html_comments(comments_data)
        else:
            # in the case of a 304, the browser cache will handle it.
            return '', 304, get_response_headers(comments_data, HTML_MIME)


@api_bp.route('/issues/<int:number>/labels', methods=['POST'])
def modify_labels(number):
    """XHR endpoint to modify issue labels.

    Sending in an empty array removes them all as well.
    This method is not proxied, so only users with write access
    will be able to edit labels.
    """
    if g.user:
        path = 'repos/{0}/{1}/labels'.format(ISSUES_PATH, number)
        return api_request('put', path, data=request.data)
    else:
        abort(403)


@api_bp.route('/issues/labels')
def get_repo_labels():
    """XHR endpoint to get all possible labels in a repo."""
    params = request.args.copy()
    path = 'repos/{0}/labels'.format(REPO_PATH)
    return api_request('get', path, params=params)
