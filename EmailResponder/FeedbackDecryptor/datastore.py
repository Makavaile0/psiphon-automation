# Copyright (c) 2013, Psiphon Inc.
# All rights reserved.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


'''
There are currently three tables in our Mongo DB:

- diagnostic_info: Holds diagnostic info sent by users. This typically includes
  info about client version, OS, server response time, etc. Data in this table
  is permanent. The idea is that we can mine it to find out relationships
  between Psiphon performance and user environment.

- email_diagnostic_info: This is a little less concrete. The short version is:
  This table indicates that a particlar diagnostic_info record should be
  formatted and emailed. It might also record additional information (like the
  email ID and subject) about the email that should be sent. Once the diagnostic_info
  has been sent, the associated record is removed from this table.

- stats: A dumb DB that is really just used for maintaining state between stats
  service restarts.
'''

import datetime
import os
from pymongo import MongoClient
import numpy
from functools import reduce

import logger

#
# The collections in our Mongo DB
#
# `diagnostic_info` holds diagnostic info sent by users. This typically includes info about
# client version, OS, server response time, etc. Data in this table is
# permanent. The idea is that we can mine it to find out relationships between
# Psiphon performance and user environment.
#
# `email_diagnostic_info` indicates that a particlar diagnostic_info record should be
# formatted and emailed. It might also record additional information (like the
# email ID and subject) about the email that should be sent. Once the
# diagnostic_info has been sent, the associated record is removed from this
# table.
#
# `stats` is a single-record collection that stores the last time a stats email was sent.
#
# `autoresponder` stores info about autoresponses that should be sent.
#
# `response_blacklist` is a time-limited store of email address to which responses have been sent. This
# is used to help us avoid sending responses to the same person more than once
# per day (or whatever).
#
# `errors` is a store of the errors we've seen. Printed into the stats email.


# We want to reuse our mongodb connection, but we need to make sure that it doesn't get copied
# into a fork. So we'll cache the connection but make sure it belongs to the current pid.
_mongo_db = None
_mongo_db_pid = None
def _db():
    global _mongo_db, _mongo_db_pid
    pid = os.getpid()
    if _mongo_db is not None and _mongo_db_pid == pid:
        return _mongo_db
    connection = MongoClient()
    _mongo_db = connection.feedback
    _mongo_db_pid = pid
    return _mongo_db


#
# Create any necessary indexes
#

# This index is used for iterating through the diagnostic_info store, and
# for stats queries.
# It's also a TTL index, and purges old records.
DIAGNOSTIC_DATA_LIFETIME_SECS = 60*60*24*7*26  # half a year
_db().diagnostic_info.create_index('datetime', expireAfterSeconds=DIAGNOSTIC_DATA_LIFETIME_SECS)

# We use a TTL index on the response_blacklist collection, to expire records.
_BLACKLIST_LIFETIME_SECS = 60*60*24  # one day
_db().response_blacklist.create_index('datetime', expireAfterSeconds=_BLACKLIST_LIFETIME_SECS)

# Add a TTL index to the errors store.
_ERRORS_LIFETIME_SECS = 60*60*24*7*26  # half a year
_db().errors.create_index('datetime', expireAfterSeconds=_ERRORS_LIFETIME_SECS)

# Add a TTL index to the email_diagnostic_info store. We don't want queued items to live
# forever, because a) we don't want to fall so far behind in email that we're only getting
# old items; and b) eventually the underlying diagnostic data will be purged from the diagnostic_info store.
_EMAIL_DIAGNOSTIC_INFO_LIFETIME_SECS = 24*60*60  # one day
_db().email_diagnostic_info.create_index('datetime', expireAfterSeconds=_EMAIL_DIAGNOSTIC_INFO_LIFETIME_SECS)

# More lookup indexes
_db().diagnostic_info.create_index('Metadata.platform')
_db().diagnostic_info.create_index('Metadata.version')
_db().diagnostic_info.create_index('Metadata.id')


#
# Functions to manipulate diagnostic info
#

def insert_diagnostic_info(obj):
    '''
    Returns _id of inserted document if successful; otherwise returns None if an
    error occurs, or the provided diagnostic info has the same id as a
    pre-existing document.
    '''
    feedback_id = obj.get("Metadata", {}).get("id", None)
    if feedback_id is None:
        logger.error("insert_diagnostic_info: missing id")
        return None

    doc = _db().diagnostic_info.find_one({"Metadata.id": feedback_id}, {"Metadata.id": 1, "_id": 0})
    if doc is not None:
        logger.error("insert_diagnostic_info: duplicate id {}".format(feedback_id))
        return None

    obj['datetime'] = datetime.datetime.utcnow()
    return _db().diagnostic_info.insert_one(obj).inserted_id


def insert_email_diagnostic_info(diagnostic_info_record_id,
                                 email_id,
                                 email_subject):
    obj = {'diagnostic_info_record_id': diagnostic_info_record_id,
           'email_id': email_id,
           'email_subject': email_subject,
           'datetime': datetime.datetime.utcnow()
           }
    return _db().email_diagnostic_info.insert_one(obj).inserted_id


def get_email_diagnostic_info_iterator():
    return _db().email_diagnostic_info.find()


def find_diagnostic_info(diagnostic_info_record_id):
    if not diagnostic_info_record_id:
        return None

    return _db().diagnostic_info.find_one({'_id': diagnostic_info_record_id})


def remove_email_diagnostic_info(email_diagnostic_info):
    _db().email_diagnostic_info.find_one_and_delete({'_id': email_diagnostic_info['_id']})


#
# Functions related to the autoresponder
#

def insert_autoresponder_entry(email_info, diagnostic_info_record_id):
    if not email_info and not diagnostic_info_record_id:
        return

    obj = {'diagnostic_info_record_id': diagnostic_info_record_id,
           'email_info': email_info,
           'datetime': datetime.datetime.utcnow()
           }
    return _db().autoresponder.insert_one(obj).inserted_id


def get_autoresponder_iterator():
    while True:
        next_rec = _db().autoresponder.find_one_and_delete(filter={})
        if not next_rec:
            return None
        yield next_rec


#
# Functions related to the email address blacklist
#

def check_and_add_response_address_blacklist(address: str) -> bool:
    '''
    Returns True if the address is blacklisted, otherwise inserts it in the DB
    and returns False.
    '''
    # Check and insert with a single command
    match = _db().response_blacklist.find_one_and_update(
        filter={'address': address},
        update={'$setOnInsert': {'datetime': datetime.datetime.utcnow()}},
        upsert=True)

    return bool(match)


#
# Functions for the stats DB
#

def set_stats_last_send_time(timestamp):
    '''
    Sets the last send time to `timestamp`.
    '''
    _db().stats.update_one({}, {'$set': {'last_send_time': timestamp}}, upsert=True)


def get_stats_last_send_time():
    rec = _db().stats.find_one()
    return rec['last_send_time'] if rec else None


def get_new_stats_count(since_time):
    assert(since_time)
    return _db().diagnostic_info.count_documents({'datetime': {'$gt': since_time}})


def get_stats(since_time):
    # The "count" queries with large time windows seem very slow, so we're going to cap
    # since_time to 1 day ago.
    day_ago = datetime.datetime.utcnow() - datetime.timedelta(days=1)
    if (not since_time) or (since_time < day_ago):
        since_time = day_ago

    ERROR_LIMIT = 500

    # The number of errors is unbounded, so we're going to limit the count.
    # We're also going to exclude errors (i.e., "duplicate id" errors) that are very
    # common and not very interesting.
    new_errors = [_clean_record(e) for e in _db().errors.find(
        {'$and': [
            {'datetime': {'$gt': since_time}},
            {'error.error': {'$not': {'$regex': 'duplicate id'}}}
        ]}).limit(ERROR_LIMIT)]

    return {
        'since_timestamp': since_time,
        'now_timestamp': datetime.datetime.utcnow(),
        'new_android_records': _db().diagnostic_info.count_documents({'datetime': {'$gt': since_time}, 'Metadata.platform': 'android'}),
        'new_windows_records': _db().diagnostic_info.count_documents({'datetime': {'$gt': since_time}, 'Metadata.platform': 'windows'}),
        'stats': _get_stats_helper(since_time),
        'new_errors': new_errors,
    }


def add_error(error):
    _db().errors.insert_one({'error': error, 'datetime': datetime.datetime.utcnow()})


def _clean_record(rec):
    '''
    Remove the _id field. Both alters the `rec` param and returns it.
    '''
    if '_id' in rec:
        del rec['_id']
    return rec


def _get_stats_helper(since_time):
    raw_stats = {}

    #
    # Different platforms and versions have different structures
    #

    cur = _db().diagnostic_info.find({'datetime': {'$gt': since_time},
                                       'Metadata.platform': 'android',
                                       'Metadata.version': 1})
    for rec in cur:
        propagation_channel_id = rec.get('SystemInformation', {})\
                                    .get('psiphonEmbeddedValues', {})\
                                    .get('PROPAGATION_CHANNEL_ID')
        sponsor_id = rec.get('SystemInformation', {})\
                        .get('psiphonEmbeddedValues', {})\
                        .get('SPONSOR_ID')

        if not propagation_channel_id or not sponsor_id:
            continue

        response_checks = [r['data'] for r in rec.get('DiagnosticHistory', [])
                           if r.get('msg') == 'ServerResponseCheck'
                             and r.get('data').get('responded') and r.get('data').get('responseTime')]

        for r in response_checks:
            if isinstance(r['responded'], str):
                r['responded'] = (r['responded'] == 'Yes')
            if isinstance(r['responseTime'], str):
                r['responseTime'] = int(r['responseTime'])

        if ('android', propagation_channel_id, sponsor_id) not in raw_stats:
            raw_stats[('android', propagation_channel_id, sponsor_id)] = {'count': 0, 'response_checks': [], 'survey_results': []}

        raw_stats[('android', propagation_channel_id, sponsor_id)]['response_checks'].extend(response_checks)
        raw_stats[('android', propagation_channel_id, sponsor_id)]['count'] += 1

    # The structure got more standardized around here.
    for platform, version in (('android', 2), ('windows', 1)):
        cur = _db().diagnostic_info.find({'datetime': {'$gt': since_time},
                                           'Metadata.platform': platform,
                                           'Metadata.version': {'$gt': version}})
        for rec in cur:
            propagation_channel_id = rec.get('DiagnosticInfo', {})\
                                        .get('SystemInformation', {})\
                                        .get('PsiphonInfo', {})\
                                        .get('PROPAGATION_CHANNEL_ID')
            sponsor_id = rec.get('DiagnosticInfo', {})\
                            .get('SystemInformation', {})\
                            .get('PsiphonInfo', {})\
                            .get('SPONSOR_ID')

            if not propagation_channel_id or not sponsor_id:
                continue

            response_checks = (r['data'] for r in rec.get('DiagnosticInfo', {}).get('DiagnosticHistory', [])
                              if r.get('msg') == 'ServerResponseCheck'
                                 and r.get('data').get('responded') and r.get('data').get('responseTime'))

            survey_results = rec.get('Feedback', {}).get('Survey', {}).get('results', [])
            if type(survey_results) != list:
                survey_results = []

            if (platform, propagation_channel_id, sponsor_id) not in raw_stats:
                raw_stats[(platform, propagation_channel_id, sponsor_id)] = {'count': 0, 'response_checks': [], 'survey_results': []}

            raw_stats[(platform, propagation_channel_id, sponsor_id)]['response_checks'].extend(response_checks)
            raw_stats[(platform, propagation_channel_id, sponsor_id)]['survey_results'].extend(survey_results)
            raw_stats[(platform, propagation_channel_id, sponsor_id)]['count'] += 1

    def survey_reducer(accum, val):
        accum.setdefault(val.get('title', 'INVALID'), {}).setdefault(val.get('answer', 'INVALID'), 0)
        accum[val.get('title', 'INVALID')][val.get('answer', 'INVALID')] += 1
        return accum

    stats = []
    for result_params, results in raw_stats.items():
        response_times = [r['responseTime'] for r in results['response_checks'] if r['responded']]
        mean = float(numpy.mean(response_times)) if len(response_times) else None
        median = float(numpy.median(response_times)) if len(response_times) else None
        stddev = float(numpy.std(response_times)) if len(response_times) else None
        quartiles = [float(q) for q in numpy.percentile(response_times, [5.0, 25.0, 50.0, 75.0, 95.0])] if len(response_times) else None
        failrate = float(len(results['response_checks']) - len(response_times)) / len(results['response_checks']) if len(results['response_checks']) else 1.0

        survey_results = reduce(survey_reducer, results['survey_results'], {})

        stats.append({
                      'platform': result_params[0],
                      'propagation_channel_id': result_params[1],
                      'sponsor_id': result_params[2],
                      'mean': mean,
                      'median': median,
                      'stddev': stddev,
                      'quartiles': quartiles,
                      'failrate': failrate,
                      'response_sample_count': len(results['response_checks']),
                      'survey_results': survey_results,
                      'record_count': results['count'],
                      })

    return stats
