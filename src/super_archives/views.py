# -*- coding: utf-8 -*-

import smtplib
import logging
import urlparse

import requests

from django import http
from django.conf import settings
from django.contrib import messages
from django.db import IntegrityError
from django.views.generic import View
from django.core.paginator import Paginator
from django.utils.translation import ugettext as _
from django.core.exceptions import ObjectDoesNotExist
from django.utils.decorators import method_decorator
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect

from . import queries
from .utils.email import send_verification_email
from .models import MailingList, Thread, EmailAddress, EmailAddressValidation


def thread(request, mailinglist, thread_token):
    if request.method == 'GET':
        return thread_get(request, mailinglist, thread_token)
    elif request.method == 'POST':
        return thread_post(request, mailinglist, thread_token)
    else:
        return HttpResponseNotAllowed(['HEAD', 'GET', 'POST'])


def thread_get(request, mailinglist, thread_token):
    try:
        first_message = queries.get_first_message_in_thread(mailinglist,
                                                            thread_token)
    except ObjectDoesNotExist:
        raise http.Http404

    thread = Thread.objects.get(subject_token=thread_token,
                                mailinglist__name=mailinglist)
    thread.hit(request)

    order_by = request.GET.get('order')
    if order_by == 'voted':
        msgs_query = queries.get_messages_by_voted()
    else:
        msgs_query = queries.get_messages_by_date()

    msgs_query = msgs_query.filter(thread__subject_token=thread_token)
    msgs_query = msgs_query.filter(thread__mailinglist__name=mailinglist)
    emails = msgs_query.exclude(id=first_message.id)

    total_votes = first_message.votes_count()
    for email in emails:
        total_votes += email.votes_count()

    # Update relevance score
    thread.update_score()

    context = {
        'first_msg': first_message,
        'emails': [first_message] + list(emails),
        'pagehits': queries.get_page_hits(request.path_info),
        'total_votes': total_votes,
        'thread': thread,
    }

    return render(request, 'message-thread.html', context)


def thread_post(request, mailinglist, thread_token):
    try:
        thread = Thread.objects.get(subject_token=thread_token,
                                    mailinglist__name=mailinglist)
    except Thread.DoesNotExist:
        raise http.Http404

    data = {}
    data['from']  = '{} <{}>'.format(request.user.get_full_name(),
                                  request.user.email)
    data['subject'] = thread.message_set.first().subject_clean
    data['body'] = request.POST.get('emailbody', '').strip()

    url = urlparse.urljoin(settings.MAILMAN_API_URL, mailinglist + '/sendmail')

    error_msg = None
    try:
        resp = requests.post(url, data=data, timeout=2)
    except requests.exceptions.ConnectionError:
        resp = None
        error_msg = _('Error trying to connect to Mailman API')
    except requests.exceptions.Timeout:
        resp = None
        error_msg = _('Timout trying to connect to Mailman API')

    if resp and resp.status_code == 200:
        messages.success(request, _("Your message was sent. It may take "
                                    "some minutes before it's delivered. "
                                    "Why don't you breath some fresh air "
                                    "in the meanwhile."))
    else:
        if not error_msg:
            if resp is not None:
                if resp.status_code == 400:
                    error_msg = _('You cannot send an empty email')
                elif resp.status_code == 404:
                    error_msg = _('Mailing list does not exist')
            else:
                error_msg = _('Unkown error trying to connect to Mailman API')
        messages.error(request, error_msg)

    return thread_get(request, mailinglist, thread_token)


def list_messages(request):
    selected_lists = request.GET.get('list', [])
    if selected_lists:
        selected_lists = selected_lists.split()

    order_by = request.GET.get('order')
    if order_by == 'hottest':
        threads = queries.get_hottest_threads()
    else:
        threads = queries.get_latest_threads()

    mail_list = selected_lists
    if mail_list:
        threads = threads.filter(mailinglist__name__in=mail_list)

    paginator = Paginator(threads, 16)
    try:
        page = int(request.GET.get('p', '1'))
    except ValueError:
        page = 1
    threads = paginator.page(page)

    lists = MailingList.objects.all()

    template_data = {
        'lists': lists,
        'n_results': paginator.count,
        'threads': threads,
        'selected_lists': ' '.join(selected_lists) if selected_lists else '',
        'order_data': settings.ORDERING_DATA,
    }
    return render(request, 'message-list.html', template_data)


class EmailView(View):

    http_method_names = [u'head', u'get', u'post', u'delete', u'update']

    def get(self, request, key):
        """Validate an email with the given key"""

        try:
            email_val = EmailAddressValidation.objects.get(validation_key=key,
                                                           user__pk=request.user.pk)
        except EmailAddressValidation.DoesNotExist:
            messages.error(request, _('The email address you are trying to '
                                      'verify either has already been verified '
                                      'or does not exist.'))
            return redirect('/')

        try:
            email = EmailAddress.objects.get(address=email_val.address)
        except EmailAddress.DoesNotExist:
            email = EmailAddress(address=email_val.address)

        if email.user:
            messages.error(request, _('The email address you are trying to '
                                      'verify is already an active email '
                                      'address.'))
            email_val.delete()
            return redirect('/')

        email.user = email_val.user
        email.save()
        email_val.delete()

        messages.success(request, _('Email address verified!'))
        return redirect('user_profile', username=email_val.user.username)


    @method_decorator(login_required)
    def post(self, request, key):
        """Create new email address that will wait for validation"""

        email = request.POST.get('email')
        if not email:
            return http.HttpResponseBadRequest()

        try:
            EmailAddressValidation.objects.create(address=email,
                                                  user=request.user)
        except IntegrityError:
            # 409 Conflict
            #   duplicated entries
            #   email exist and it's waiting for validation
            return http.HttpResponse(status=409)

        return http.HttpResponse(status=201)

    @method_decorator(login_required)
    def delete(self, request, key):
        """Remove an email address, validated or not."""

        request.DELETE = http.QueryDict(request.body)
        email_addr = request.DELETE.get('email')

        if not email_addr:
            return http.HttpResponseBadRequest()

        try:
            email = EmailAddressValidation.objects.get(address=email_addr,
                                                       user=request.user)
        except EmailAddressValidation.DoesNotExist:
            pass
        else:
            email.delete()
            return http.HttpResponse(status=204)

        try:
            email = EmailAddress.objects.get(address=email_addr,
                                             user=request.user)
        except EmailAddress.DoesNotExist:
            raise http.Http404

        email.user = None
        email.save()
        return http.HttpResponse(status=204)

    @method_decorator(login_required)
    def update(self, request, key):
        """Set an email address as primary address."""

        request.UPDATE = http.QueryDict(request.body)

        email_addr = request.UPDATE.get('email')
        if not email_addr:
            return http.HttpResponseBadRequest()

        try:
            email = EmailAddress.objects.get(address=email_addr,
                                             user=request.user)
        except EmailAddress.DoesNotExist:
            raise http.Http404

        request.user.email = email_addr
        request.user.save()
        return http.HttpResponse(status=204)


class EmailValidationView(View):

    http_method_names = [u'post']

    def post(self, request):
        email_addr = request.POST.get('email')
        try:
            email = EmailAddressValidation.objects.get(address=email_addr,
                                                       user=request.user)
        except http.DoesNotExist:
            raise http.Http404

        try:
            send_verification_email(email_addr, request.user,
                                    email.validation_key)
        except smtplib.SMTPException:
            logging.exception('Error sending validation email')
            return http.HttpResponseServerError()

        return http.HttpResponse(status=204)
