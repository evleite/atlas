#!/usr/bin/env python

import re
import os
import time
import logging
import textwrap
from datetime import datetime

from flask import request, Response, current_app, abort, request, jsonify
from webargs import fields
from webargs.flaskparser import use_args
from jira import JIRA, JIRAError

from atlas.api import api_v1_blueprint as bp
from atlas.extensions import redis

log = logging.getLogger('api.webhook')

jira_key_re = re.compile(r'[A-Z]+-\d+')

webhook_args = {
    'token': fields.Str(required=True),
    'team_id': fields.Str(),
    'team_domain': fields.Str(),
    'channel_id': fields.Str(),
    'channel_name': fields.Str(required=True),
    'timestamp': fields.Float(),
    'user_id': fields.Str(),
    'user_name': fields.Str(required=True),
    'text': fields.Str(required=True),
    'trigger_word': fields.Str(),
}


def _mark_seen(channel, key):
    key = '%s.%s' % (channel, key)
    ttl = current_app.config['JIRA_ID_BLACKOUT_PERIOD']
    redis.setex(key, time.time(), ttl)


def _issue_seen(channel, key):
    key = '%s.%s' % (channel, key)
    return redis.get(key)


@bp.route('/webhooks/jira', methods=['POST'])
@use_args(webhook_args)
def on_msg(args):
    if args['token'] not in current_app.config['SLACK_WEBHOOK_TOKENS']:
        log.warning('Invalid token: %s', args['token'])
        abort(401)

    if args['user_name'] == 'slackbot':
        # Avoid infinite feedback loop of bot parsing it's own messages :)
        return Response()

    channel = args['channel_name']

    issue_keys = jira_key_re.findall(args['text'])
    if issue_keys:
        log.info('Message from %s in #%s contained JIRA issue key(s): %s',
                 args['user_name'], channel, ', '.join(issue_keys))

        # Login to JIRA
        authinfo = (
            current_app.config['JIRA_USERNAME'],
            current_app.config['JIRA_PASSWORD'],
        )
        jira_url = current_app.config['JIRA_URL']
        options = {'check_update': False}
        jira = JIRA(jira_url, basic_auth=authinfo, options=options)

        # Retrieve issue(s)
        issue_text = []
        for issue_key in issue_keys:
            try:
                last_mention = _issue_seen(channel, issue_key)
                if last_mention:
                    date = datetime.utcfromtimestamp(float(last_mention))
                    log.debug('%s last mentioned in #%s at %s', issue_key, channel, date)
                    continue
                issue = jira.issue(issue_key)
                issue_text.append(get_formatted_issue_message(issue))
                _mark_seen(channel, issue_key)
            except JIRAError as e:
                if e.status_code == 404:
                    log.warning('%s does not exist', issue_key)
                else:
                    log.error('Error looking up %s: %s', issue_key, e.text)

        if issue_text:
            return jsonify({
                'text': '\n\n'.join(issue_text),
            })

    return Response()


def get_formatted_issue_message(issue):
    message = textwrap.dedent("""\
    *{issue.key}:* {issue.fields.summary}
    `{issue.fields.issuetype.name}` - `{issue.fields.priority.name}` - `{issue.fields.status.name}`
    """.format(issue=issue))
    if issue.fields.assignee:
        message += textwrap.dedent("""\
        Assigned to: {issue.fields.assignee.displayName}
        """.format(issue=issue))
    message += os.path.join(
        current_app.config['JIRA_URL'],
        'browse',
        issue.key
    )
    message = message.rstrip('\n')
    return message
