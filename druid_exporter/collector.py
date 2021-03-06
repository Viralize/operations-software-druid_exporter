# Copyright 2017 Luca Toscano
#                Filippo Giunchedi
#                Wikimedia Foundation
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import queue
import threading

from collections import defaultdict
from prometheus_client.core import (CounterMetricFamily, GaugeMetricFamily,
                                    HistogramMetricFamily, Summary)


log = logging.getLogger(__name__)

try:
    from kafka import KafkaConsumer
except ImportError:
    KafkaConsumer = None


class DruidCollector(object):
    scrape_duration = Summary(
            'druid_scrape_duration_seconds', 'Druid scrape duration')

    def __init__(self, metrics_config, kafka_config=None):

        # The ingestion of the datapoints is separated from their processing,
        # to separate concerns and avoid unnecessary slowdowns for Druid
        # daemons sending data.
        # Only one thread de-queues and process datapoints, in this way we
        # don't really need any special locking to guarantee consistency.
        # Since this thread is not I/O bound it doesn't seem the case to
        # use a gevent's greenlet, but more tests might prove the contrary.
        self.datapoints_queue = queue.Queue()
        self.stop_threads = threading.Event()

        threading.Thread(
                target=self.process_queued_datapoints,
                args=(self.stop_threads,)).start()

        # if a Kafka config is provided, create a dedicated thread
        # that pulls datapoints from a Kafka topic.
        # The thread will then push datapoints to the same queue that
        # the HTTP server uses. In this way the exporter allows a mixed
        # configuration for Druid Brokers between HTTPEmitter and
        # KafkaEmitter (for daemons emitting too many datapoints/s).
        if kafka_config:
            if KafkaConsumer:
                threading.Thread(
                    target=self.pull_datapoints_from_kafka,
                    args=(kafka_config, self.stop_threads)).start()
            else:
                log.error('A Kafka configuration was provided, but it seems '
                          'that the Kafka client library is not available. '
                          'Please install the correct dependencies.')

        # Datapoints successfully registered
        self.datapoints_registered = 0

        # Data structure holding histogram data
        # Format: {daemon: {metric_name: {bucket2: value, bucket2: value, ...}}
        self.histograms = defaultdict(lambda: {})

        # Data structure holding counters data
        # Format: {daemon: {label_name: {label2_name: value}}
        # The order of the labels listed in supported_metric_names is important
        # since it is reflected in this data structure. The layering is not
        # strictly important for the final prometheus metrics but
        # it is simplifies the code that creates them (collect method).
        self.counters = defaultdict(lambda: {})

        # List of metrics to collect/expose via the exporter
        self.metrics_config = metrics_config
        self.supported_daemons = list(self.metrics_config.keys())

    def stop_running_threads(self):
        self.stop_threads.set()

    @staticmethod
    def sanitize_field(datapoint_field):
        return datapoint_field.replace('druid/', '').lower()

    def store_counter(self, datapoint):
        """ This function adds data to the self.counters dictiorary
            following its convention, creating on the fly
            the missing bits. For example, given:
            self.counters = {}
            datapoint = {'service': 'druid/broker',
                         'metric'='segment/size',
                         'datasource': 'test', 'value': 10}

            This function will creates the following:
            self.counters = {
                'segment/size': {
                    'broker':
                        { ('test'): 10 }
                    }
                }

            The algorithm is generic enough to support all metrics handled by
            self.counters without caring about the number of labels needed.
        """
        daemon = DruidCollector.sanitize_field(str(datapoint['service']))
        metric_name = str(datapoint['metric'])
        metric_value = float(datapoint['value'])

        metrics_storage = self.counters[metric_name]
        metric_labels = self.metrics_config[daemon][metric_name]['labels']

        metrics_storage.setdefault(daemon, {})

        label_values = []
        if metric_labels:
            for label in metric_labels:
                try:
                    label_values.append(str(datapoint[label]))
                except KeyError as e:
                    log.error('Missing label {} for datapoint {} (expected labels: {}), '
                              'dropping it. Please check your metric configuration file.'
                              .format(label, datapoint, metric_labels))
                    return

        # Convert the list of labels to a tuple to allow indexing
        metrics_storage[daemon][tuple(label_values)] = metric_value
        log.debug("The datapoint {} modified the counters dictionary to: \n{}"
                  .format(datapoint, self.counters))

    def store_histogram(self, datapoint):
        """ Store datapoints that will end up in histogram buckets using a dictiorary.
            This function is highly customized for the only histograms configured
            so far, rather than being generic like store_counter. Example of how
            it works:
            self.histograms = {}
            datapoint = {'service': 'druid/broker', 'metric'='query/time',
                         'datasource': 'test', 'value': 10}

            This function will creates the following:
            self.histograms = {
                'query/time': {
                    'broker': {
                        ('test'): {'10': 1, '100': 1, etc.., 'sum': 10 }
                    }
                }
            }
        """
        daemon = DruidCollector.sanitize_field(str(datapoint['service']))
        metric_name = str(datapoint['metric'])
        metric_value = float(datapoint['value'])
        metric_labels = self.metrics_config[daemon][metric_name]['labels']
        metric_buckets = self.metrics_config[daemon][metric_name]['buckets']

        self.histograms.setdefault(metric_name, {daemon: {}})
        self.histograms[metric_name].setdefault(daemon, {})

        label_values = []
        if metric_labels:
            for label in metric_labels:
                try:
                    label_values.append(str(datapoint[label]))
                except KeyError as e:
                    log.error('Missing label {} for datapoint {} (expected labels: {}), '
                              'dropping it. Please check your metric configuration file.'
                              .format(label, metric_labels, datapoint))
                    return

        # Convert the list of labels to a tuple to allow indexing
        self.histograms[metric_name][daemon].setdefault(tuple(label_values), {})

        stored_buckets = self.histograms[metric_name][daemon][tuple(label_values)]
        for bucket in metric_buckets:
            if bucket not in stored_buckets:
                stored_buckets[bucket] = 0
            if bucket != 'sum' and metric_value <= float(bucket):
                stored_buckets[bucket] += 1
        stored_buckets['sum'] += metric_value

        log.debug("The datapoint {} modified the histograms dictionary to: \n{}"
                  .format(datapoint, self.histograms))

    @scrape_duration.time()
    def collect(self):
        # Loop through all metrics configured, and get datapoints
        # for them saved by the exporter.
        for daemon in self.metrics_config.keys():
            for druid_metric_name in self.metrics_config[daemon]:
                metric_type = self.metrics_config[daemon][druid_metric_name]['type']

                if metric_type == 'gauge' or metric_type == 'counter':
                    try:
                        self.counters[druid_metric_name]
                        self.counters[druid_metric_name][daemon]
                    except KeyError:
                        continue

                    if metric_type == 'gauge':
                        metric_family_obj = GaugeMetricFamily
                    else:
                        metric_family_obj = CounterMetricFamily

                    prometheus_metric = metric_family_obj(
                        self.metrics_config[daemon][druid_metric_name]['prometheus_metric_name'],
                        self.metrics_config[daemon][druid_metric_name]['description'],
                        labels=map(
                            lambda x: x.lower(),
                            self.metrics_config[daemon][druid_metric_name]['labels']))
                    label_values = list(self.counters[druid_metric_name][daemon].keys())
                    for label_value in label_values:
                        value = self.counters[druid_metric_name][daemon][label_value]
                        prometheus_metric.add_metric(label_value, value)

                elif metric_type == 'histogram':
                    try:
                        self.histograms[druid_metric_name]
                        self.histograms[druid_metric_name][daemon]
                    except KeyError:
                        continue

                    prometheus_metric = HistogramMetricFamily(
                            self.metrics_config[daemon][druid_metric_name]['prometheus_metric_name'],
                            self.metrics_config[daemon][druid_metric_name]['description'],
                            labels=map(
                                lambda x: x.lower(),
                                self.metrics_config[daemon][druid_metric_name]['labels']))

                    label_values = list(self.histograms[druid_metric_name][daemon].keys())
                    for label_value in label_values:
                        value = self.histograms[druid_metric_name][daemon][label_value]
                        buckets_without_sum = [
                            [key, value] for key, value in value.items() if key != 'sum']
                        prometheus_metric.add_metric(
                            label_value, buckets=buckets_without_sum, sum_value=value['sum'])

                else:
                    log.info('metric type not supported: {}'.format(metric_type))
                    continue

                yield prometheus_metric

        registered = CounterMetricFamily('druid_exporter_datapoints_registered',
                                         'Number of datapoints successfully registered '
                                         'by the exporter.')
        registered.add_metric([], self.datapoints_registered)
        yield registered

    def register_datapoint(self, datapoint):
        if (datapoint['feed'] != 'metrics'):
            log.debug("The following feed does not contain a datapoint, "
                      "dropping it: {}"
                      .format(datapoint))
            return

        daemon = DruidCollector.sanitize_field(str(datapoint['service']))
        if (datapoint['feed'] != 'metrics' or
                daemon not in self.supported_daemons or
                datapoint['metric'] not in self.metrics_config[daemon].keys()):
            log.debug("The following datapoint is not supported, either "
                      "because the 'feed' field is not 'metrics' or "
                      "the daemon name ({}) is not listed in the supported ones ({}) or "
                      "the metric itself is not listed in the exporter's config file: {}"
                      .format(daemon, self.supported_daemons, datapoint))
            return

        self.datapoints_queue.put((daemon, datapoint))

    def process_queued_datapoints(self, stop_threads):
        log.debug('Process datapoints thread starting..')

        while True and not stop_threads.isSet():
            (daemon, datapoint) = self.datapoints_queue.get()
            metric_name = str(datapoint['metric'])
            if self.metrics_config[daemon][metric_name]['type'] == 'histogram':
                self.store_histogram(datapoint)
            else:
                self.store_counter(datapoint)

            self.datapoints_registered += 1

        log.debug('Process datapoints thread shutting down..')

    def pull_datapoints_from_kafka(self, kafka_config, stop_threads):
        log.debug('Kafka datapoints puller thread starting..')

        consumer = KafkaConsumer(
            kafka_config['topic'],
            group_id=kafka_config['group_id'],
            bootstrap_servers=kafka_config['bootstrap_servers'])

        while True and not stop_threads.isSet():
            consumer.poll()
            for message in consumer:
                try:
                    json_message = json.loads(message.value.decode())
                    log.debug('Datapoint from kafka: %s', json_message)
                    if type(json_message) == list:
                        for datapoint in json_message:
                            self.register_datapoint(datapoint)
                    else:
                        self.register_datapoint(json_message)
                except json.JSONDecodeError:
                    log.exception("Failed to decode message from Kafka, skipping..")
                except Exception as e:
                    log.exception("Generic exception while pulling datapoints from Kafka")

        log.debug('Kafka datapoints puller thread shutting down..')
