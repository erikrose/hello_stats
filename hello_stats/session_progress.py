#!/usr/bin/env python
"""Determine how often it occurs that 2 people are attempting to be in a room
together and actually succeed in communicating. Write that data to an S3
bucket so a dashboard can pull it out.

Usage::

    ES_URL=... ES_USERNAME=... ES_PASSWORD=... python state_histogram.py \
      [--no-publish] [--beginning-of-time YYYY-MM-DD]

Specifically, for each set of overlapping join-leave spans in a room, what is
the furthest state the link-clicker and the built-in client both reach? We emit
a histogram of the answer to that question.

Idealized state sequences for the 2 different types of clients::

    Unregistered/Registered:
        action = join refresh* leave
        state = <none>

    Link-clicker:
        action = join (status / refresh)+ leave
        state = waiting starting receiving sending? sendrecv  # These can come out
                                                              # of order due to varying
                                                              # latencies in transports.

    Iff action=status, there's a state.

"""
from collections import OrderedDict
from cPickle import UnpicklingError
from datetime import datetime, timedelta, date
from os import environ
from os.path import dirname, join

from pyelasticsearch import ElasticSearch, ElasticHttpNotFoundError

from hello_stats.events import BEGINNING_OF_TIME, EVENT_CLASSES_WORST_FIRST, events_from_day
from hello_stats.events import Connected, Success, SendRecv
from hello_stats.sessions import World, Room
from hello_stats.storage import VERSION, PickleBucket, VersionedJsonBucket
import argparse


class StateCounter(object):
    """A histogram of link-clicker states"""

    def __init__(self, buckets=(), sort=False):
        """If you want a bucket to be guaranteed to show up, pass it in as one
        of ``buckets``. Otherwise, I'll make them dynamically.

        If sort is false, the order of the buckets will be used.
        """
        self.total = 0
        self.d = OrderedDict((b, 0) for b in buckets)
        self.sort = sort

    def incr(self, state):
        self.d[state] = self.d.get(state, 0) + 1
        self.total += 1

    def add(self, otherCounter):
        """Adds another counter's totals into this counter."""
        for k, e in otherCounter.d.items():
            self.d[k] = self.d.get(k, 0) + e
            self.total += e

    def histogram(self, stars=100):
        """Return an ASCII-art bar chart for debugging and exploration."""
        ret = []
        STARS = 100

        if self.sort:
            items = sorted(self.d.iteritems())
        else:
            items = self.d.iteritems()

        for state, count in items:
            ret.append('{state: >9} {bar} {count}'.format(
                state=state,
                bar='*' * (STARS * count / (self.total or 1)),
                count=count))
        return '\n'.join(ret)

    def __str__(self):
        """Distribute 100 stars over all the state, modulo rounding errors."""
        return self.histogram()

    def as_dict(self):
        """Return a dictionary with a key for total and for every bucket."""
        return dict(total=self.total, **self.d)

    def __nonzero__(self):
        return self.total != 0


class DayConnectionTimesSummary(object):
    """Builds a connection time summary based on supplied times."""
    def __init__(self):
        self.clickerAvTime = []
        self.clickerScreenTime = []
        self.builtInAvTime = []
        self.exceptionNumbers = StateCounter(sort=True)
        self.exceptionTimes = []

    def append(self, clickerAvTime, clickerScreenTime, builtInAvTime):
        self.clickerAvTime.append(clickerAvTime)
        self.clickerScreenTime.append(clickerScreenTime)
        self.builtInAvTime.append(builtInAvTime)

    def appendException(self, exceptionNumber, exceptionTime):
        self.exceptionNumbers.incr(exceptionNumber)
        self.exceptionTimes.append(exceptionTime)

    def generate_counter(self, times):
        counter = StateCounter(sort=True)

        for time in times:
            counter.incr(time)

        return counter

    def print_summary(self):
        print "Clicker A/V Connection Times:"
        print self.generate_counter(self.clickerAvTime)

        print "Clicker Screen Connection Times:"
        print self.generate_counter(self.clickerScreenTime)

        print "Built-in A/V Connection Times:"
        print self.generate_counter(self.builtInAvTime)

        print "First exception numbers:"
        print self.exceptionNumbers

        print "Exception Times (link-clicker & built-in):"
        print self.generate_counter(self.exceptionTimes)


class PeriodConnectionTimesSummary(object):
    """Builds a connection time summary based over multiple days by summing
       results from DayConnectionTimesSummary.
    """
    def __init__(self):
        self.clickerAvTime = []
        self.clickerScreenTime = []
        self.builtInAvTime = []
        self.exceptionNumbers = StateCounter(sort=True)
        self.exceptionTimes = []

    def append(self, dayTimesSummary):
        self.clickerAvTime += dayTimesSummary.clickerAvTime
        self.clickerScreenTime += dayTimesSummary.clickerScreenTime
        self.builtInAvTime += dayTimesSummary.builtInAvTime
        self.exceptionNumbers.add(dayTimesSummary.exceptionNumbers)
        self.exceptionTimes += dayTimesSummary.exceptionTimes

    def generate_counter(self, times):
        counter = StateCounter(sort=True)

        for time in times:
            counter.incr(time)

        return counter

    def print_stats(self, times):
        # Get rid of "zero times", we couldn't determine anything.
        noZerosTimes = [t for t in times if t > 0 and t < 35]

        count = len(noZerosTimes)
        if not count:
            print "No values available"
            return

        print "Total Points: %d" % len(noZerosTimes)
        print "Min         : %d" % min(noZerosTimes)
        print "Average     : %.2f" % (sum(noZerosTimes) / float(count))
        print "Max         : %d" % max(noZerosTimes)

        print self.generate_counter(noZerosTimes)

    def print_summary(self):
        print "Clicker A/V Connection Metrics"
        print "------------------------------"
        self.print_stats(self.clickerAvTime)
        print "Clicker Screen Connection Metrics"
        print "---------------------------------"
        self.print_stats(self.clickerScreenTime)
        print "Built-in A/V Connection Metrics"
        print "-------------------------------"
        self.print_stats(self.builtInAvTime)
        print "First Exception Numbers"
        print "-----------------------"
        print self.exceptionNumbers
        print "Times to first exception (link-clicker & built-in, timed from streamCreated event)"
        print "----------------------------------------------------------------------------------"
        self.print_stats(self.exceptionTimes)


def days_between(start, end):
    """Yield each datetime.date in the interval [start, end)."""
    while start < end:
        yield start
        start += timedelta(days=1)  # safe across DST because dates have no concept of hours


def counts_for_day(segments):
    """Return a StateCounter conveying a histogram of the segments' furthest
    states."""
    counter = StateCounter(c.name() for c in EVENT_CLASSES_WORST_FIRST)
    dayCounter = DayConnectionTimesSummary()

    for segment in segments:
        furthest = segment.furthest_state()
        counter.incr(furthest.name())
        # Additional logging to work out if the connection times if we connected
        # successfully.
        if furthest is Connected or furthest is Success:
            clickerAvTime, clickerScreenTime, builtInAvTime = segment.get_connection_time_integer()
            dayCounter.append(clickerAvTime, clickerScreenTime, builtInAvTime)

        # Additional logging to work out the time to the first exception, if the
        # segment didn't connect successfully.
        if furthest is SendRecv and segment.exception():
            exceptionTime = segment.get_exception_time_integer()
            dayCounter.appendException(segment.exception(), exceptionTime)

    return counter, dayCounter


def success_duration_histogram(segments):
    """Return an iterable of lengths of time it takes to get from tryst to
    sendrecv."""
    # Answer: 90% in <7s, 99% in <23s, from running against most of 8/13/2015.
    # I guess I want to see if the failures are more slanted toward short intervals than this histogram.
    for segment in segments:
        room = Room()
        start = None
        for event in segment:
            room.do(event)
            if start is None and room.in_session:
                start = event.timestamp
            if isinstance(event, SendRecv):
                if start is None:
                    yield 0  # Weirdness. Timestamp slop?
                else:
                    yield (event.timestamp - start).seconds
                break


def failure_duration_histogram(segments):
    """Return an iterable of at-least durations from when 2 people tryst to when
    there's only 1 person in the room.

    Actual duration is *at least* what's returned, almost certainly more, but
    we don't have easy access to the finishing event, which is in the next
    segment. But the point here is to see if there are an unusual number of 0s
    in the output, as in things didn't have enough time to negotiate a
    connection.

    """
    # A full 52% of these come out as 0. That suggests a lot failures could be due
    # to having insufficient time for negotiation (though, of course, it really
    # means "at least 0", not exactly 0, so take that into account. Next, it
    # would be nice to get actual numbers for this, not just "at least" ones.
    for segment in segments:
        room = Room()
        start = None
        for event in segment:
            room.do(event)
            if room.in_session:
                start = event.timestamp
                break
        if start is not None:  # otherwise, 2 people never met. Impossible?
            yield (segment[-1].timestamp - start).seconds
        else:
            yield "Inconceivable!"


def update_metrics(es, version, metrics, world, beginning_of_time):
    """Update metrics with today's (and previous missed days') data.

    Also update the state of the ``world`` with sessions that may hang over
    into tomorrow.

    If VERSION has increased or ``metrics`` is empty, start over.

    """
    today = date.today()

    if not metrics or VERSION > version:  # need to start over
        start_at = beginning_of_time
        metrics = []
        periodTimesSummary = PeriodConnectionTimesSummary()
        world = World()
    else:
        # Figure out which days we missed, as of the end of the stored JSON.
        # (This tolerates unreliable cron jobs, which Heroku warns of, and
        # also guards against other transient failures.)
        start_at = datetime.strptime(metrics[-1]['date'], '%Y-%m-%d').date() + timedelta(days=1)

    # Add each of those to the bucket:
    for day in days_between(start_at, today):
        iso_day = day.isoformat()
        print "Computing furthest-state histogram for %s..." % iso_day

        try:
            segments = world.do(events_from_day(iso_day, es))
            counts, dayCounter = counts_for_day(segments)
        except ElasticHttpNotFoundError:
            print 'Index not found. Proceeding to next day.'
            continue
        print counts
        print "%s sessions span midnight (%s%%)." % (len(world._rooms), len(world._rooms) / float(counts.total) * 100)

        dayCounter.print_summary()

        a_days_metrics = counts.as_dict()
        a_days_metrics['date'] = iso_day
        metrics.append(a_days_metrics)

        periodTimesSummary.append(dayCounter)

    return metrics, world, periodTimesSummary


def valid_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return argparse.ArgumentTypeError("Not a valid date: %s" % s)


def main():
    parser = argparse.ArgumentParser(
        description="Loop locale chrome manifest generation script")
    parser.add_argument("-b", "--beginning-of-time",
                        default=BEGINNING_OF_TIME,
                        type=valid_date,
                        help="The start date for the beginning of metrics processing, "
                        "currently %s. Format YYYY-MM-DD" % BEGINNING_OF_TIME.isoformat())
    parser.add_argument("--no-publish",
                        default=False,
                        action="store_true",
                        help="Don't try to publish the metrics output")
    args = parser.parse_args()

    """Pull the JSON of the historical metrics out of S3, compute the new ones
    up through yesterday, and write them back to S3.

    We can assume the JSON is small enough to handle because it's already
    being pulled into the browser in its entirety, along with a dozen other
    datasets, to display the dashboard.

    """
    es = ElasticSearch(environ['ES_URL'],
                       username=environ['ES_USERNAME'],
                       password=environ['ES_PASSWORD'],
                       ca_certs=join(dirname(__file__), 'mozilla-root.crt'),
                       timeout=600)

    if args.no_publish:
        world = None
        version = None
        metrics = []
    else:
        # Get previous metrics and midnight-spanning room state from buckets:
        metrics_bucket = VersionedJsonBucket(
            bucket_name='net-mozaws-prod-metrics-data',
            key='loop-server-dashboard/loop_full_room_progress.json',
            access_key_id=environ['METRICS_ACCESS_KEY_ID'],
            secret_access_key=environ['METRICS_SECRET_ACCESS_KEY'])
        version, metrics = metrics_bucket.read()
        world_bucket = PickleBucket(
            bucket_name='mozilla-loop-metrics-state',
            key='session-progress.pickle',
            access_key_id=environ['STATE_ACCESS_KEY_ID'],
            secret_access_key=environ['STATE_SECRET_ACCESS_KEY'])
        try:
            world = world_bucket.read()
        except (UnpicklingError, AttributeError):
            world = None
            metrics = []

    metrics, world, periodTimesSummary = \
        update_metrics(es, version, metrics, world, args.beginning_of_time)

    if not args.no_publish:
        # Write back to the buckets:
        world_bucket.write(world)
        metrics_bucket.write(metrics)

    periodTimesSummary.print_summary()


if __name__ == '__main__':
    main()


# Observations:
#
# * Most rooms never see 2 people meet: 20K lonely rooms vs. 1500 meeting ones.
# * There are some sessions in which leaves happen without symmetric joins.
#   See if these occur near the beginning of days. Otherwise, I would expect
#   at least Refreshes every 5 minutes.
# * These numbers may be a little high because we're assuming all
#   link-clickers are the same link-clicker. When we start logging sessionID,
#   we can start distinguishing them. (hostname is the IP of the server, not
#   of the client.)
# * We could be nice and not expect a sendrecv to happen if the co-presence of
#   2 people lasts only a few seconds. Maybe we could chart the length of failed
#   sessions and figure out where n sigmas is.
