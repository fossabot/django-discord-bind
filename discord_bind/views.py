"""

The MIT License (MIT)

Copyright (c) 2016, Mark Rogaski

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

"""
from __future__ import unicode_literals
import random
import string
from datetime import datetime

from django.http import HttpResponseRedirect, HttpResponseForbidden
try:
    from django.urls import reverse
except ImportError:
    from django.core.urlresolvers import reverse

from django.utils.timezone import make_aware
from django.db.models import Q
from django.contrib import messages
from django.contrib.auth import authenticate, login
from django.core.exceptions import PermissionDenied

import requests
from requests_oauthlib import OAuth2Session

from discord_bind.models import DiscordUser, DiscordInvite
from discord_bind.conf import settings

import logging
logger = logging.getLogger(__name__)


def oauth_session(request, state=None, token=None):
    """ Constructs the OAuth2 session object. """
    if settings.DISCORD_REDIRECT_URI is not None:
        redirect_uri = settings.DISCORD_REDIRECT_URI
    else:
        redirect_uri = request.build_absolute_uri(
            reverse('discord_bind_callback'))
    scope = (['identify', 'email', 'guilds'] if settings.DISCORD_EMAIL_SCOPE
             else ['identify', 'guilds'])
    if settings.DISCORD_INVITE_SCOPE:
        scope.append("guilds.join")
    if request.GET.get("raise_email", False):
        if 'email' not in scope:
            scope.append('email')
    return OAuth2Session(settings.DISCORD_CLIENT_ID,
                         redirect_uri=redirect_uri,
                         scope=scope,
                         token=token,
                         state=state)


def index(request):
    # Record the final redirect alternatives
    if 'invite_uri' in request.GET:
        request.session['discord_bind_invite_uri'] = request.GET['invite_uri']
    else:
        request.session['discord_bind_invite_uri'] = (
                settings.DISCORD_INVITE_URI)

    if 'return_uri' in request.GET:
        request.session['discord_bind_return_uri'] = request.GET['return_uri']
    else:
        request.session['discord_bind_return_uri'] = (
                settings.DISCORD_RETURN_URI)

    if 'next' in request.GET:
        request.session['discord_bind_next_uri'] = request.GET.get('next', "/")
    else:
        request.session['discord_bind_next_uri'] = "/"

    # Compute the authorization URI
    oauth = oauth_session(request)
    url, state = oauth.authorization_url(settings.DISCORD_BASE_URI +
                                         settings.DISCORD_AUTHZ_PATH)
    request.session['discord_bind_oauth_state'] = state
    return HttpResponseRedirect(url)


def callback(request):
    def decompose_data(user, token):
        """ Extract the important details """
        data = {
            'uid': user['id'],
            'username': user['username'],
            'discriminator': user['discriminator'],
            'email': user.get('email', ''),
            'avatar': user.get('avatar', ''),
            'access_token': token['access_token'],
            'refresh_token': token.get('refresh_token', ''),
            'scope': ' '.join(token.get('scope', '')),
        }
        for k in data:
            if data[k] is None:
                data[k] = ''
        try:
            expiry = datetime.utcfromtimestamp(float(token['expires_at']))
            if settings.USE_TZ:
                expiry = make_aware(expiry)
            data['expiry'] = expiry
        except KeyError:
            pass
        return data

    def bind_user(request, data):
        """ Create or update a DiscordUser instance """
        uid = data.pop('uid')

        remote_user = authenticate(request, remote_user=str(uid))

        if remote_user is not None:
            login(request, remote_user)
        else:
            return HttpResponseRedirect("/?login_error=true")

        usr_count = DiscordUser.objects.filter(uid=uid).update(user=remote_user, **data)
        if usr_count == 0:
            new_user = DiscordUser(uid=uid, user=remote_user, **data)
            new_user.save()
            print("ERROR: " + new_user.username)

    response = request.build_absolute_uri()
    state = request.session['discord_bind_oauth_state']
    if request.GET.get('error', False):
        if request.GET.get('error', "") == "access_denied":
            if settings.DISCORD_ERROR_URI:
                redir_url = settings.DISCORD_ERROR_URI
            else:
                redir_url = "/"
            return HttpResponseRedirect("{}?error=access_denied".format(redir_url))
    if 'state' not in request.GET or request.GET['state'] != state:
        raise PermissionDenied
    oauth = oauth_session(request, state=state)
    token = oauth.fetch_token(settings.DISCORD_BASE_URI +
                              settings.DISCORD_TOKEN_PATH,
                              client_secret=settings.DISCORD_CLIENT_SECRET,
                              authorization_response=response)

    # Get Discord user data
    user = oauth.get(settings.DISCORD_BASE_URI + '/users/@me').json()
    data = decompose_data(user, token)
    bind_user(request, data)

    # Accept Discord invites
    groups = request.user.groups.all()
    invites = DiscordInvite.objects.filter(active=True).filter(
                                        Q(groups__in=groups) | Q(groups=None))
    count = 0
    for invite in invites:
        r = oauth.post(settings.DISCORD_BASE_URI + '/invites/' + invite.code)
        if r.status_code == requests.codes.ok:
            count += 1
            logger.info(('accepted Discord '
                         'invite for %s/%s') % (invite.guild_name,
                                                invite.channel_name))
        else:
            logger.error(('failed to accept Discord '
                          'invite for %s/%s: %d %s') % (invite.guild_name,
                                                        invite.channel_name,
                                                        r.status_code,
                                                        r.reason))

    # Select return target
    if count > 0:
        messages.success(request, '%d Discord invite(s) accepted.' % count)
        url = request.session['discord_bind_next_uri']
        # url = request.session['discord_bind_invite_uri']
    else:
        url = request.session['discord_bind_next_uri']

    # Clean up
    del request.session['discord_bind_oauth_state']
    del request.session['discord_bind_invite_uri']
    del request.session['discord_bind_return_uri']
    del request.session['discord_bind_next_uri']

    return HttpResponseRedirect(url)
