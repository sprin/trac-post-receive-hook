#!/usr/bin/python -W ignore::DeprecationWarning

# trac-post-receive-hook
# ----------------------------------------------------------------------------
# Copyright (c) 2004 Stephen Hansen
# Copyright (c) 2009 Sebastian Noack
# paradigm shift by sprin in 2012
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to
# deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in
#   all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.
# ----------------------------------------------------------------------------

# This git post-receive hook script is meant to interface to the
# Trac (http://www.edgewall.com/products/trac/) issue tracking/wiki/etc
# system. It is based on the Subversion post-commit hook, part of Trac 0.11.
#
# The script is written for users of ticket branches.
# With essentially no cost in time or space to create branches,
# one or more branches can be created for a given dev ticket.
# By convention, these branches are prefixed with the ticket number,
# for example, `5150_myfeature`. Devs are encouraged to journal
# their work as commits and push to the server frequently.
#
# This post-receive hook will take all new commits to a ticket branch,
# pretty-format and combine messages, and post them to the ticket.
# The script will make one comment per ticket per push. There is
# nothing to stop one from pushing more than one branch, thus triggering
# comments to multiple tickets.
#
# Merge commits will be posted to the ticket that corresponds to the
# branch being merged in (source). The assumption is the target branch is
# a `master` or `develop` branch, which has no corresponding ticket.
#
# It is also allowed to insert a "Refs #999"-style token in the message.
# This is encouraged for the case where it is desired to post a message
# to a ticket other than the one corresponding to the ticket branch that it is
# being committed to.
#
# TODO: A future improvement is to handle the case where the merge target is
# is the ticket branch. It should also be possible to handle the case of
# merging two feature branches and post to both tickets.
#
# A multiline commit message will display nicely, thus giving more
# value to thoughtful implementation notes in commit messages.
#
# Commits to branches that do not match the pattern are posted to a
# default ticket which is configurable. The repo administrator
# can subscribe to this ticket to watch for unexpected behaviour
# on the part of the hook or users.
#
# INSTALLATION:
# Place this file in the host's .git/hooks/ directory of the
# git repository. It must be named `post-receive`.
# Make the file executable: `chmod +x post-receive`.
# Set the shebang in the first line to the desired python executable.
#
# SETTINGS:
# TRAC_ENV - point at trac environment created with `trac-admin myenv initenv`
# GIT_PATH - point at git executable
# REPOST_SEEN - if False, do not repost commits already seen.
# POST_COMMENT - if True, post a comment.
# VERBOSE: - print debug messages.
#
# TESTING:
# This can be tested without making a push to the server.
#
# Set up for testing:
# Set REPOST_SEEN to True, POST_COMMENT to False, VERBOSE to True.
# Create a branch, beginning with a ticket number, which can be
# deleted later. Make some empty commits with `git commit --allow-empty`.
# Use `git log` to get the 40-byte SHA1 of the oldest and newest
# commits made.
#
# Trigger the post-commit script:
# $ echo $oldsha1 $newsha1 refs/heads/branchname | .git/hooks/post-receive
# echo simulates the output the script would normally get from
# stdin when git triggers the post-receive.
#
# Test comment posting:
# If the output matches your expectation, set POST_COMMENT to True.
# Create a ticket and make a new branch prefixed with that ticket number.
# Make more empty commits, get the SHA1, and trigger the script again.
# Verify the ticket has a comment.
#
# DEPLOYMENT:
# Set REPOST_SEEN to False.
# Set POST_COMMENT to True.
# Set VERBOSE to False.

from collections import defaultdict
import sys
import os
import re
from subprocess import Popen, PIPE
from datetime import datetime
from operator import itemgetter
import psycopg2

# Use the egg cache of the environment if not other python egg cache is given.
if not 'PYTHON_EGG_CACHE' in os.environ:
    os.environ['PYTHON_EGG_CACHE'] = '/tmp/.egg-cache'

# Regex for finding ticket number in refname.
ticket_from_ref = r'^refs/heads/([0-9]+)'
ticket_from_ref_re = re.compile(ticket_from_ref)

# This regex will optionally capture the merge target if a ticket branch
ticket_from_msg = r'Merge branch \'([0-9]+)(?:\w*\' into ([0-9]+))?'
ticket_from_msg_re = re.compile(ticket_from_msg)

ticket_from_explicit_refs_re = re.compile(r'Refs #([0-9]+)')

def call_git(command, args):
    """Return result of calling git with args."""
    return Popen([GIT_PATH, command] + args, stdout=PIPE).communicate()[0]

def get_commit_message(commit, env):

    if VERBOSE:
        print "handling commit: %s" % commit

    msg = call_git('log', ['--pretty=format:%s' % PRETTY_FORMAT,
        "-1", commit]).rstrip()
    if VERBOSE:
        print msg
    return msg

def post_to_ticket(msg, author, tkt_id, env):
    """Post the message to the ticket and send a notify email."""
    from trac.ticket.notification import TicketNotifyEmail
    from trac.ticket import Ticket
    from trac.ticket.web_ui import TicketModule
    from trac.util.datefmt import utc

    now = datetime.now(utc)

    try:
        db = env.get_db_cnx()
        # Get the related trac ticket object
        ticket = Ticket(env, tkt_id, db)

        # determine sequence number...
        cnum = 0
        tm = TicketModule(env)
        for change in tm.grouped_changelog_entries(ticket, db):
            if change['permanent']:
                cnum += 1

        ticket.save_changes(author, msg, now, db, cnum + 1)
        db.commit()

        tn = TicketNotifyEmail(env)
        tn.notify(ticket, newticket=0, modtime=now)
    except Exception, e:
        msg = 'Unexpected error processing ticket ID %s: %s' % (tkt_id, e)
        print >>sys.stderr, msg

def handle_ref(old, new, ref, env):
    """Handle all the new commits to the ref."""

    from trac.util.text import to_unicode

    if VERBOSE:
        print ref
    # Regex the ticket number out of the refname
    match = ticket_from_ref_re.search(ref)

    tkt_id_from_ref = DEFAULT_POST_RECEIVE_TKT_ID
    if match:
        tkt_id_from_ref = int(match.group(1))

        if VERBOSE:
            print "Parsed ticket from refname: %s" % tkt_id_from_ref
    # Get the list of hashs for commits in the changeset.
    args = (old == '0' * 40) and [new] or [new, '^' + old]

    pending_commits = call_git('rev-list', args).splitlines()
    if VERBOSE:
        print "pending commits: %s" % pending_commits
    if not pending_commits:
        return

    # Get the subset of pending commits that are already seen.
    db = env.get_db_cnx()
    cursor = db.cursor()

    try:
        cursor.execute('SELECT sha1 FROM git_seen WHERE sha1 IN (%s)'
            % ', '.join(['%s'] * len(pending_commits)), pending_commits)
        seen_commits = map(itemgetter(0), cursor.fetchall())
    except psycopg2.ProgrammingError:
        # almost definitely due to git_seen missing
        cursor.close()
        db.close()
        # get a new cursor
        db = env.get_db_cnx()
        cursor = db.cursor()
        cursor.execute('CREATE TABLE git_seen (sha1 VARCHAR(40));')
        seen_commits = []

    ticket_msgs = defaultdict(list)
    # Iterate over commits, starting with earliest
    for commit in reversed(pending_commits):
        # If the commit was seen already, we do not repost it.
        if commit in seen_commits and not REPOST_SEEN:
           continue

        remember_commit(commit, db, cursor)

        # Get message from commit
        msg = get_commit_message(commit, env)

        # First check for explicit "Refs #999"-style ticket refs.
        matches = ticket_from_explicit_refs_re.findall(msg)
        for ticket_id in matches:
            ticket_msgs[ticket_id].append(to_unicode(msg))

        # If a merge commit, try to identify origin ticket.
        match = ticket_from_msg_re.search(msg)
        if match:
            source_tkt_id = int(match.group(1))
            target_tkt_id = match.group(2)
            ticket_msgs[source_tkt_id].append(to_unicode(msg))
            if target_tkt_id:
                ticket_msgs[int(target_tkt_id)].append(to_unicode(msg))
        else:
        # Otherwise, we comment on the ticket corresponding to the ref
            ticket_msgs[tkt_id_from_ref].append(to_unicode(msg))

    # the wire (hook) hears all
    author = "the wire"

    try:
        if POST_COMMENT:
            for tkt_id, commit_msgs in ticket_msgs.items():
                print "Posting to ticket #%s" % tkt_id
                post_to_ticket('\n----\n'.join(commit_msgs),
                               author, tkt_id, env)
    except Exception, e:
        msg = 'Unexpected error processing commit %s: %s' % (commit[:7], e)
        print >>sys.stderr, msg
        db.rollback()
    else:
        db.commit()

def remember_commit(commit, db, cursor):
    # Remember commit, so each commit is only processed once.
    try:
         cursor.execute('INSERT INTO git_seen (sha1) VALUES (%s)', [commit])
    except db.IntegrityError:
         # If an integrity error occurs (perhaps because another process
         # has triggered the script in the meantime), skip the insert.
         pass


if __name__ == '__main__':
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    # Import project-specific configuration variables
    from hook_config import *

    from trac.env import open_environment
    env = open_environment(TRAC_ENV)

    # A post-receive gets on stdin a line for each ref updated of the format:
    # <oldsha1> <newsha1> <ref-name>
    # eg:
    # 53f...dc4 b38...057 refs/heads/master
    for line in sys.stdin:
        handle_ref(env=env, *line.split())
