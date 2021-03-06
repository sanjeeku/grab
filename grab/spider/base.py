# FIXME: split to modules, make smaller
# pylint: disable=too-many-lines
import logging
import time
from random import randint
from copy import deepcopy
import os
from traceback import format_exc
from datetime import datetime
from threading import Thread
from collections import deque

from six.moves import queue
import six
from weblib import metric
from weblib.error import ResponseNotValid

from grab.base import Grab
from grab.error import GrabInvalidUrl
from grab.spider.error import (SpiderError, SpiderMisuseError, FatalError,
                               NoTaskHandler, NoDataHandler,
                               SpiderConfigurationError)
from grab.spider.task import Task
from grab.spider.data import Data
from grab.proxylist import ProxyList, BaseProxySource
from grab.util.misc import camel_case_to_underscore
from grab.base import GLOBAL_STATE
from grab.stat import Stat, Timer
from grab.spider.parser_pipeline import ParserPipeline
from grab.spider.cache_pipeline import CachePipeline
from grab.util.warning import warn

DEFAULT_TASK_PRIORITY = 100
DEFAULT_NETWORK_STREAM_NUMBER = 3
DEFAULT_TASK_TRY_LIMIT = 5
DEFAULT_NETWORK_TRY_LIMIT = 5
RANDOM_TASK_PRIORITY_RANGE = (50, 100)
NULL = object()

# pylint: disable=invalid-name
logger = logging.getLogger('grab.spider.base')
logger_verbose = logging.getLogger('grab.spider.base.verbose')
# pylint: disable=invalid-name
# If you need verbose logging just
# change logging level of that logger
logger_verbose.setLevel(logging.FATAL)


class SpiderMetaClass(type):
    """
    This meta class does following things::

    * It creates Meta attribute, if it is not defined in
        Spider descendant class, by copying parent's Meta attribute
    * It reset Meta.abstract to False if Meta is copied from parent class
    * If defined Meta does not contains `abstract`
        attribute then define it and set to False
    """

    def __new__(mcs, name, bases, namespace):
        if 'Meta' not in namespace:
            for base in bases:
                if hasattr(base, 'Meta'):
                    # copy contents of base Meta
                    meta = type('Meta', (object,), dict(base.Meta.__dict__))
                    # reset abstract attribute
                    meta.abstract = False
                    namespace['Meta'] = meta
                    break

        # Process special case (SpiderMetaClassMixin)
        if 'Meta' not in namespace:
            namespace['Meta'] = type('Meta', (object,), {})

        if not hasattr(namespace['Meta'], 'abstract'):
            namespace['Meta'].abstract = False

        return super(SpiderMetaClass, mcs).__new__(mcs, name, bases, namespace)


class TaskGeneratorWrapperThread(Thread):
    """
    Load new tasks from `self.task_generator_object`
    Create new tasks.

    If task queue size is less than some value
    then load new tasks from tasks file.
    """

    def __init__(self, real_generator, spider, *args, **kwargs):
        from threading import Event

        self.real_generator = real_generator
        self.spider = spider
        self.is_paused = Event()
        self.activity_paused = Event()
        super(TaskGeneratorWrapperThread, self).__init__(*args, **kwargs)

    def pause(self):
        self.is_paused.set()
        while not self.activity_paused.is_set():
            time.sleep(0.01)
            if not self.isAlive():
                break
        #self.activity_paused.wait()

    def resume(self):
        self.is_paused.clear()
        # Wait for `if self.is_puased.is_set()` branch
        # to be completed
        while self.activity_paused.is_set():
            if not self.isAlive():
                break
            time.sleep(0.01)

    def run(self):
        while True:
            if self.is_paused.is_set():
                self.activity_paused.set()
                while self.is_paused.is_set():
                    time.sleep(0.01)
                self.activity_paused.clear()
            with self.spider.timer.log_time('task_generator'):
                queue_size = self.spider.task_queue.size()
                min_limit = self.spider.thread_number * 10
            if queue_size < min_limit:
                with self.spider.timer.log_time('task_generator'):
                    logger_verbose.debug(
                        'Task queue contains less tasks (%d) than '
                        'allowed limit (%d). Trying to add '
                        'new tasks.', queue_size, min_limit)
                    try:
                        for _ in six.moves.range(min_limit - queue_size):
                            if self.is_paused.is_set():
                                break
                            item = next(self.real_generator)
                            logger_verbose.debug('Got new item from'
                                                 ' generator. Processing it.')
                            self.spider.process_handler_result(item, None)
                    except StopIteration:
                        # If generator have no values to yield
                        # then disable it
                        logger_verbose.debug('Task generator has no more'
                                             ' tasks. Disabling it')
                        break
            else:
                time.sleep(0.1)


@six.add_metaclass(SpiderMetaClass)
class Spider(object):
    """
    Asynchronous scraping framework.
    """
    spider_name = None

    # You can define here some urls and initial tasks
    # with name "initial" will be created from these
    # urls
    # If the logic of generating initial tasks is complex
    # then consider to use `task_generator` method instead of
    # `initial_urls` attribute
    initial_urls = None

    class Meta:
        # pylint: disable=no-init
        #
        # Meta.abstract means that this class will not be
        # collected to spider registry by `grab crawl` CLI command.
        # The Meta is inherited by descendant classes BUT
        # Meta.abstract is reset to False in each descendant
        abstract = True

    # *************
    # Class Methods
    # *************

    @classmethod
    def update_spider_config(cls, config):
        pass

    @classmethod
    def get_spider_name(cls):
        if cls.spider_name:
            return cls.spider_name
        else:
            return camel_case_to_underscore(cls.__name__)

    # **************
    # Public Methods
    # **************

    def __init__(self, thread_number=None,
                 network_try_limit=None, task_try_limit=None,
                 request_pause=NULL,
                 priority_mode='random',
                 meta=None,
                 only_cache=False,
                 config=None,
                 args=None,
                 # New options start here
                 taskq=None,
                 # MP:
                 network_result_queue=None,
                 parser_result_queue=None,
                 is_parser_idle=None,
                 shutdown_event=None,
                 mp_mode=False,
                 parser_pool_size=None,
                 parser_mode=False,
                 parser_requests_per_process=10000,
                 # http api
                 http_api_port=None,
                 transport='multicurl',
                 grab_transport='pycurl'):
        """
        Arguments:
        * thread-number - Number of concurrent network streams
        * network_try_limit - How many times try to send request
            again if network error was occurred, use 0 to disable
        * network_try_limit - Limit of tries to execute some task
            this is not the same as network_try_limit
            network try limit limits the number of tries which
            are performed automatically in case of network timeout
            of some other physical error
            but task_try_limit limits the number of attempts which
            are scheduled manually in the spider business logic
        * priority_mode - could be "random" or "const"
        * meta - arbitrary user data
        * retry_rebuild_user_agent - generate new random user-agent for each
            network request which is performed again due to network error
        * args - command line arguments parsed with `setup_arg_parser` method
        New options:
        * taskq=None,
        * newtork_response_queue=None,
        """

        # API:
        self.http_api_port = http_api_port

        assert transport in ('multicurl', 'threaded')
        self.transport_name = transport

        assert grab_transport in ('pycurl', 'urllib3')
        self.grab_transport_name = grab_transport

        # MP:
        self.mp_mode = mp_mode
        if self.mp_mode:
            from multiprocessing import Event, Queue
        else:
            from multiprocessing.dummy import Event, Queue

        if network_result_queue is not None:
            self.network_result_queue = network_result_queue
        else:
            self.network_result_queue = Queue()
        self.parser_result_queue = parser_result_queue
        self.is_parser_idle = is_parser_idle
        if shutdown_event is not None:
            self.shutdown_event = shutdown_event
        else:
            self.shutdown_event = Event()
        if not self.mp_mode and parser_pool_size and parser_pool_size > 1:
            raise SpiderConfigurationError(
                'Parser pool size could be only 1 in '
                'non-multiprocess mode')
        self.parser_pool_size = parser_pool_size
        self.parser_mode = parser_mode
        self.parser_requests_per_process = parser_requests_per_process

        self.stat = Stat()
        self.timer = Timer()
        self.task_queue = taskq

        if args is None:
            self.args = {}
        else:
            self.args = args

        if config is not None:
            self.config = config
        else:
            self.config = {}

        if meta:
            self.meta = meta
        else:
            self.meta = {}

        self.thread_number = (
            thread_number or
            int(self.config.get('thread_number',
                                DEFAULT_NETWORK_STREAM_NUMBER)))
        self.task_try_limit = (
            task_try_limit or
            int(self.config.get('task_try_limit', DEFAULT_TASK_TRY_LIMIT)))
        self.network_try_limit = (
            network_try_limit or
            int(self.config.get('network_try_limit',
                                DEFAULT_NETWORK_TRY_LIMIT)))

        self._grab_config = {}
        if priority_mode not in ['random', 'const']:
            raise SpiderMisuseError('Value of priority_mode option should be '
                                    '"random" or "const"')
        else:
            self.priority_mode = priority_mode

        self.only_cache = only_cache
        self.work_allowed = True
        if request_pause is not NULL:
            warn('Option `request_pause` is deprecated and is not '
                 'supported anymore')

        self.proxylist_enabled = None
        self.proxylist = None
        self.proxy = None
        self.proxy_auto_change = False
        self.interrupted = False

        self._task_generator_list = []
        self.cache_pipeline = None
        self.parser_pipeline = None
        self.transport = None

    def setup_cache(self, backend='mongo', database=None, use_compression=True,
                    **kwargs):
        """
        Setup cache.

        :param backend: Backend name
            Should be one of the following: 'mongo', 'mysql' or 'postgresql'.
        :param database: Database name.
        :param kwargs: Additional credentials for backend.

        """
        if database is None:
            raise SpiderMisuseError('setup_cache method requires database '
                                    'option')
        mod = __import__('grab.spider.cache_backend.%s' % backend,
                         globals(), locals(), ['foo'])
        cache = mod.CacheBackend(database=database,
                                 use_compression=use_compression,
                                 spider=self, **kwargs)
        self.cache_pipeline = CachePipeline(self, cache)

    def setup_queue(self, backend='memory', **kwargs):
        """
        Setup queue.

        :param backend: Backend name
            Should be one of the following: 'memory', 'redis' or 'mongo'.
        :param kwargs: Additional credentials for backend.
        """
        logger.debug('Using %s backend for task queue', backend)
        mod = __import__('grab.spider.queue_backend.%s' % backend,
                         globals(), locals(), ['foo'])
        self.task_queue = mod.QueueBackend(spider_name=self.get_spider_name(),
                                           **kwargs)

    def add_task(self, task, raise_error=False):
        """
        Add task to the task queue.
        """

        # MP:
        # ***
        if self.parser_mode:
            self.parser_result_queue.put((task, None))
            return

        if self.task_queue is None:
            raise SpiderMisuseError('You should configure task queue before '
                                    'adding tasks. Use `setup_queue` method.')
        if task.priority is None or not task.priority_set_explicitly:
            task.priority = self.generate_task_priority()
            task.priority_set_explicitly = False
        else:
            task.priority_set_explicitly = True

        if not task.url.startswith(('http://', 'https://', 'ftp://',
                                    'file://', 'feed://')):
            self.stat.collect('task-with-invalid-url', task.url)
            msg = ('It is not allowed to build Task object with '
                   'relative URL: %s' % task.url)
            ex = SpiderError(msg)
            if raise_error:
                raise ex
            else:
                # Just want to print traceback
                # Do this to avoid the error
                # http://bugs.python.org/issue23003
                # FIXME: use something less awkward
                try:
                    raise ex
                except SpiderError as ex:
                    logger.error('', exc_info=ex)
                return False

        # TODO: keep original task priority if it was set explicitly
        # WTF the previous comment means?
        self.task_queue.put(task, task.priority,
                            schedule_time=task.schedule_time)
        return True

    def stop(self):
        """
        This method set internal flag which signal spider
        to stop processing new task and shuts down.
        """

        logger_verbose.debug('Method `stop` was called')
        self.work_allowed = False

    def load_proxylist(self, source, source_type=None, proxy_type='http',
                       auto_init=True, auto_change=True):
        """
        Load proxy list.

        :param source: Proxy source.
            Accepts string (file path, url) or ``BaseProxySource`` instance.
        :param source_type: The type of the specified source.
            Should be one of the following: 'text_file' or 'url'.
        :param proxy_type:
            Should be one of the following: 'socks4', 'socks5' or'http'.
        :param auto_change:
            If set to `True` then automatical random proxy rotation
            will be used.


        Proxy source format should be one of the following (for each line):
            - ip:port
            - ip:port:login:password

        """
        self.proxylist = ProxyList()
        if isinstance(source, BaseProxySource):
            self.proxylist.set_source(source)
        elif isinstance(source, six.string_types):
            if source_type == 'text_file':
                self.proxylist.load_file(source, proxy_type=proxy_type)
            elif source_type == 'url':
                self.proxylist.load_url(source, proxy_type=proxy_type)
            else:
                raise SpiderMisuseError('Method `load_proxylist` received '
                                        'invalid `source_type` argument: %s'
                                        % source_type)
        else:
            raise SpiderMisuseError('Method `load_proxylist` received '
                                    'invalid `source` argument: %s'
                                    % source)

        self.proxylist_enabled = True
        self.proxy = None
        if not auto_change and auto_init:
            self.proxy = self.proxylist.get_random_proxy()
        self.proxy_auto_change = auto_change

    def process_next_page(self, grab, task, xpath,
                          resolve_base=False, **kwargs):
        """
        Generate task for next page.

        :param grab: Grab instance
        :param task: Task object which should be assigned to next page url
        :param xpath: xpath expression which calculates list of URLS
        :param **kwargs: extra settings for new task object

        Example::

            self.follow_links(grab, 'topic', '//div[@class="topic"]/a/@href')
        """
        try:
            # next_url = grab.xpath_text(xpath)
            next_url = grab.doc.select(xpath).text()
        except IndexError:
            return False
        else:
            url = grab.make_url_absolute(next_url, resolve_base=resolve_base)
            page = task.get('page', 1) + 1
            grab2 = grab.clone()
            grab2.setup(url=url)
            task2 = task.clone(task_try_count=1, grab=grab2,
                               page=page, **kwargs)
            self.add_task(task2)
            return True

    def render_stats(self, timing=True):
        out = ['------------ Stats: ------------']
        out.append('Counters:')

        # Process counters
        items = sorted(self.stat.counters.items(),
                       key=lambda x: x[0], reverse=True)
        for item in items:
            out.append('  %s: %s' % item)
        out.append('')

        out.append('Lists:')
        # Process collections sorted by size desc
        col_sizes = [(x, len(y)) for x, y in self.stat.collections.items()]
        col_sizes = sorted(col_sizes, key=lambda x: x[1], reverse=True)
        for col_size in col_sizes:
            out.append('  %s: %d' % col_size)
        out.append('')

        # Process extra metrics
        if 'download-size' in self.stat.counters:
            out.append('Network download: %s' %
                       metric.format_traffic_value(
                           self.stat.counters['download-size']))
        out.append('Queue size: %d' % self.task_queue.size()
                   if self.task_queue else 'NA')
        out.append('Network streams: %d' % self.thread_number)
        elapsed = self.timer.timers['total']
        hours, seconds = divmod(elapsed, 3600)
        minutes, seconds = divmod(seconds, 60)
        out.append('Time elapsed: %d:%d:%d (H:M:S)' % (
            hours, minutes, seconds))
        out.append('End time: %s' %
                   datetime.utcnow().strftime('%d %b %Y, %H:%M:%S UTC'))

        if timing:
            out.append('')
            out.append(self.render_timing())
        return '\n'.join(out) + '\n'

    def render_timing(self):
        out = ['Timers:']
        out.append('  DOM: %.3f' % GLOBAL_STATE['dom_build_time'])
        time_items = [(x, y) for x, y in self.timer.timers.items()]
        time_items = sorted(time_items, key=lambda x: x[1])
        for time_item in time_items:
            out.append('  %s: %.03f' % time_item)
        return '\n'.join(out) + '\n'

    # ********************************
    # Methods for spider customization
    # ********************************

    def prepare(self):
        """
        You can do additional spider customization here
        before it has started working. Simply redefine
        this method in your Spider class.
        """

    def prepare_parser(self):
        """
        You can do additional spider customization here
        before it has started working. Simply redefine
        this method in your Spider class.

        This method is called only from Spider working in parser mode
        that, in turn, is spawned automatically by main spider proces
        working in multiprocess mode.
        """

    def shutdown(self):
        """
        You can override this method to do some final actions
        after parsing has been done.
        """

        pass

    def update_grab_instance(self, grab):
        """
        Use this method to automatically update config of any
        `Grab` instance created by the spider.
        """
        pass

    def create_grab_instance(self, **kwargs):
        # Back-ward compatibility for deprecated `grab_config` attribute
        # Here I use `_grab_config` to not trigger warning messages
        kwargs['transport'] = self.grab_transport_name
        if self._grab_config and kwargs:
            merged_config = deepcopy(self._grab_config)
            merged_config.update(kwargs)
            grab = Grab(**merged_config)
        elif self._grab_config and not kwargs:
            grab = Grab(**self._grab_config)
        else:
            grab = Grab(**kwargs)
        return grab

    def task_generator(self):
        """
        You can override this method to load new tasks smoothly.

        It will be used each time as number of tasks
        in task queue is less then number of threads multiplied on 2
        This allows you to not overload all free memory if total number of
        tasks is big.
        """

        if False: # pylint: disable=using-constant-test
            # Some magic to make this function empty generator
            yield ':-)'
        return

    # ***************
    # Private Methods
    # ***************

    def check_task_limits(self, task):
        """
        Check that task's network & try counters do not exceed limits.

        Returns:
        * if success: (True, None)
        * if error: (False, reason)

        """

        if task.task_try_count > self.task_try_limit:
            return False, 'task-try-count'

        if task.network_try_count > self.network_try_limit:
            return False, 'network-try-count'

        return True, None

    def generate_task_priority(self):
        if self.priority_mode == 'const':
            return DEFAULT_TASK_PRIORITY
        else:
            return randint(*RANDOM_TASK_PRIORITY_RANGE)

    def start_task_generators(self):
        """
        Process `self.initial_urls` list and `self.task_generator`
        method.
        """

        logger_verbose.debug('Processing initial urls')
        if self.initial_urls:
            for url in self.initial_urls: # pylint: disable=not-an-iterable
                self.add_task(Task('initial', url=url))

        self._task_generator_list = []
        thread = TaskGeneratorWrapperThread(self.task_generator(), self)
        thread.daemon = True
        thread.start()
        self._task_generator_list.append(thread)

    def get_task_from_queue(self):
        try:
            with self.timer.log_time('task_queue'):
                return self.task_queue.get()
        except queue.Empty:
            size = self.task_queue.size()
            if size:
                logger_verbose.debug(
                    'No ready-to-go tasks, Waiting for '
                    'scheduled tasks (%d)', size)
                return True
            else:
                logger_verbose.debug('Task queue is empty.')
                return None

    def setup_grab_for_task(self, task):
        grab = self.create_grab_instance()
        if task.grab_config:
            grab.load_config(task.grab_config)
        else:
            grab.setup(url=task.url)

        # Generate new common headers
        grab.config['common_headers'] = grab.common_headers()
        self.update_grab_instance(grab)
        grab.setup_transport(self.grab_transport_name)
        return grab

    def is_valid_network_response_code(self, code, task):
        """
        Answer the question: if the response could be handled via
        usual task handler or the task failed and should be processed as error.
        """

        return (code < 400 or code == 404 or
                code in task.valid_status)

    def process_handler_error(self, func_name, ex, task):
        self.stat.inc('spider:error-%s' % ex.__class__.__name__.lower())

        if hasattr(ex, 'tb'):
            logger.error('Error in %s function', func_name)
            logger.error(ex.tb)
        else:
            logger.error('Error in %s function', func_name, exc_info=ex)

        # Looks strange but I really have some problems with
        # serializing exception into string
        try:
            ex_str = six.text_type(ex)
        except TypeError:
            try:
                ex_str = ex.decode('utf-8', 'ignore')
            except TypeError:
                ex_str = str(ex)

        task_url = task.url if task is not None else None
        self.stat.collect('fatal', '%s|%s|%s|%s' % (
            func_name, ex.__class__.__name__, ex_str, task_url))
        if isinstance(ex, FatalError):
            # raise FatalError()
            # six.reraise(FatalError, ex)
            # logger.error(ex.tb)
            raise ex

    def find_data_handler(self, data):
        try:
            return getattr(data, 'handler')
        except AttributeError:
            try:
                handler = getattr(self, 'data_%s' % data.handler_key)
            except AttributeError:
                raise NoDataHandler('No handler defined for Data %s'
                                    % data.handler_key)
            else:
                return handler

    def run_parser(self):
        """
        Main work cycle of spider process working in parser-mode.
        """
        self.is_parser_idle.clear()
        # Use Stat instance that does not print any logging messages
        if self.parser_mode:
            self.stat = Stat(logging_period=None)
        self.prepare_parser()
        process_request_count = 0
        try:
            work_permitted = True
            while work_permitted:
                try:
                    result = self.network_result_queue.get(block=False)
                except queue.Empty:
                    self.is_parser_idle.set()
                    time.sleep(0.1)
                    self.is_parser_idle.clear()
                    logger_verbose.debug('Network result queue is empty')
                    if self.shutdown_event.is_set():
                        logger_verbose.debug('Got shutdown event')
                        return
                else:
                    process_request_count += 1
                    if self.parser_mode:
                        self.stat.reset()
                    try:
                        handler = self.find_task_handler(result['task'])
                    except NoTaskHandler as ex:
                        ex.tb = format_exc()
                        self.parser_result_queue.put((ex, result['task']))
                        self.stat.inc('parser:handler-not-found')
                    else:
                        self.process_network_result(result, handler)
                        self.stat.inc('parser:handler-processed')
                    finally:
                        if self.parser_mode:
                            data = {
                                'type': 'stat',
                                'counters': self.stat.counters,
                                'collections': self.stat.collections,
                            }
                            self.parser_result_queue.put((data,
                                                          result['task']))
                        if self.parser_mode:
                            if self.parser_requests_per_process:
                                if (process_request_count >=
                                        self.parser_requests_per_process):
                                    work_permitted = False
        except Exception as ex:
            logging.error('', exc_info=ex)
            raise

    def process_network_result(self, result, handler):
        handler_name = getattr(handler, '__name__', 'NONE')
        try:
            with self.timer.log_time('response_handler'):
                with self.timer.log_time('response_handler.%s' % handler_name):
                    handler_result = handler(result['grab'], result['task'])
                    if handler_result is None:
                        pass
                    else:
                        for something in handler_result:
                            self.parser_result_queue.put((something,
                                                          result['task']))
        except NoDataHandler as ex:
            ex.tb = format_exc()
            self.parser_result_queue.put((ex, result['task']))
        except Exception as ex: # pylint: disable=broad-except
            ex.tb = format_exc()
            self.parser_result_queue.put((ex, result['task']))

    def find_task_handler(self, task):
        callback = task.get('callback')
        if callback:
            return callback
        else:
            try:
                handler = getattr(self, 'task_%s' % task.name)
            except AttributeError:
                raise NoTaskHandler('No handler or callback defined for '
                                    'task %s' % task.name)
            else:
                return handler

    def log_network_result_stats(self, res, from_cache=False):
        # Increase stat counters
        self.stat.inc('spider:request-processed')
        self.stat.inc('spider:task')
        self.stat.inc('spider:task-%s' % res['task'].name)
        if (res['task'].network_try_count == 1 and
                res['task'].task_try_count == 1):
            self.stat.inc('spider:task-%s-initial' % res['task'].name)

        # Update traffic statistics
        if res['grab'] and res['grab'].doc:
            doc = res['grab'].doc
            self.timer.inc_timer('network-name-lookup', doc.name_lookup_time)
            self.timer.inc_timer('network-connect', doc.connect_time)
            self.timer.inc_timer('network-total', doc.total_time)
            if from_cache:
                self.stat.inc('spider:download-size-with-cache',
                              doc.download_size)
                self.stat.inc('spider:upload-size-with-cache',
                              doc.upload_size)
            else:
                self.stat.inc('spider:download-size', doc.download_size)
                self.stat.inc('spider:upload-size', doc.upload_size)

    def process_grab_proxy(self, task, grab):
        """Assign new proxy from proxylist to the task"""

        if task.use_proxylist:
            if self.proxylist_enabled:
                # Need this to work around
                # pycurl feature/bug:
                # pycurl instance uses previously connected proxy server
                # even if `proxy` options is set with another proxy server
                grab.setup(connection_reuse=False)
                if self.proxy_auto_change:
                    self.change_active_proxy(task, grab)
                if self.proxy:
                    grab.setup(proxy=self.proxy.get_address(),
                               proxy_userpwd=self.proxy.get_userpwd(),
                               proxy_type=self.proxy.proxy_type)

    # pylint: disable=unused-argument
    def change_active_proxy(self, task, grab):
        self.proxy = self.proxylist.get_random_proxy()
    # pylint: enable=unused-argument

    def submit_task_to_transport(self, task, grab):
        if self.only_cache:
            self.stat.inc('spider:request-network-disabled-only-cache')
        else:
            grab_config_backup = grab.dump_config()
            self.process_grab_proxy(task, grab)
            self.stat.inc('spider:request-network')
            self.stat.inc('spider:task-%s-network' % task.name)
            with self.timer.log_time('network_transport'):
                logger_verbose.debug('Submitting task to the transport '
                                     'layer')
                try:
                    self.transport.start_task_processing(
                        task, grab, grab_config_backup)
                except GrabInvalidUrl:
                    logger.debug('Task %s has invalid URL: %s',
                                 task.name, task.url)
                    self.stat.collect('invalid-url', task.url)

    def start_api_thread(self):
        from grab.spider.http_api import HttpApiThread

        proc = HttpApiThread(self)
        proc.start()
        return proc

    def is_ready_to_shutdown(self):
        # Things should be true to shutdown spider
        # 1) No active task handlers (task_* functions)
        # 2) All task generators has completed work
        # 3) No active network threads
        # 4) Task queue is empty
        # 5) Network result queue is empty
        # 6) Cache is disabled or is in idle mode

        # print('parser result queue', self.parser_result_queue.qsize())
        # print('all parsers are idle', all(x['is_parser_idle'].is_set()
        #                                  for x in (self.parser_pipeline
        #                                            .parser_pool)))
        # print('alive task generators',
        #       any(x.isAlive() for x in self._task_generator_list))
        # print('active network threads',
        #       self.transport.get_active_threads_number())
        #print('!IS READY: cache is idle: %s' % self.cache_pipeline.is_idle())
        try:
            if self.cache_pipeline:
                self.cache_pipeline.pause()
            for th in self._task_generator_list:
                if th.isAlive():
                    th.pause()
            return (
                not self.parser_result_queue.qsize()
                and all(x['is_parser_idle'].is_set()
                        for x in self.parser_pipeline.parser_pool)
                and not any(x.isAlive() for x
                            in self._task_generator_list)  # (2)
                and not self.transport.get_active_threads_number()  # (3)
                and not self.task_queue.size()  # (4)
                and not self.network_result_queue.qsize()  # (5)
                and (self.cache_pipeline is None
                     or self.cache_pipeline.is_idle())
            )
        finally:
            #print('!resuming cache')
            if self.cache_pipeline:
                self.cache_pipeline.resume()
            for th in self._task_generator_list:
                if th.isAlive():
                    th.resume()

    def run(self):
        """
        Main method. All work is done here.
        """

        if self.mp_mode:
            from multiprocessing import Queue
        else:
            from multiprocessing.dummy import Queue

        self.timer.start('total')

        if self.transport_name == 'multicurl':
            from grab.spider.transport.multicurl import MulticurlTransport

            self.transport = MulticurlTransport(self, self.thread_number)
        elif self.transport_name == 'threaded':
            from grab.spider.transport.threaded import ThreadedTransport

            self.transport = ThreadedTransport(self, self.thread_number)

        if self.http_api_port:
            http_api_proc = self.start_api_thread()
        else:
            http_api_proc = None

        self.parser_result_queue = Queue()
        self.parser_pipeline = ParserPipeline(
            bot=self,
            mp_mode=self.mp_mode,
            pool_size=self.parser_pool_size,
            shutdown_event=self.shutdown_event,
            network_result_queue=self.network_result_queue,
            parser_result_queue=self.parser_result_queue,
            requests_per_process=self.parser_requests_per_process,
        )
        network_result_queue_limit = max(10, self.thread_number * 2)

        try:
            # Run custom things defined by this specific spider
            # By defaut it does nothing
            self.prepare()

            # Setup task queue if it has not been configured yet
            if self.task_queue is None:
                self.setup_queue()

            # Initiate task generator. Only in main process!
            with self.timer.log_time('task_generator'):
                self.start_task_generators()

            # Work in infinite cycle untill
            # `self.work_allowed` flag is True
            # shutdown_countdown = 0 # !!!
            pending_tasks = deque()
            shutdown_countdown = 10
            while self.work_allowed:
                #print('!')
                # Load new task only if:
                # 1) network transport has free threads
                # 2) network result queue is not full
                # 3) cache is disabled OR cache has free resources
                if (self.transport.get_free_threads_number()
                        and (self.network_result_queue.qsize()
                             < network_result_queue_limit)
                        and (self.cache_pipeline is None
                             or self.cache_pipeline.has_free_resources())):
                    if pending_tasks:
                        task = pending_tasks.popleft()
                    else:
                        task = self.get_task_from_queue()
                        #if task and task is not True:
                        #    print('NEW TASK: %s [delay=%s]'
                        #          % (task, task.original_delay))
                    #print('!asked for new task, got %s' % task)
                    if task is None:
                        # If received task is None then
                        # check if spider is ready to be shut down
                        if not pending_tasks and self.is_ready_to_shutdown():
                            #print('!ready-to-shutdown is OK')
                            # I am afraid there is a bug in
                            # `is_ready_to_shutdown`
                            # because it tries to evaluate too many things
                            # includig things that are being set from other
                            # threads, # so to ensure we are really ready to
                            # shutdown # I call # is_ready_to_shutdown a few
                            # more times.
                            # Without this hack some times really rarely times
                            # the Grab fails to do its job
                            # A good way to see this bug is to disable
                            # this hack and run:
                            # while ./runtest.py -t test.spider_data; do \
                            # echo "ok"; done;
                            # And wait a few minutes
                            # Iterate over all run body while waiting
                            # this safety time
                            # this is required because the code emulates async
                            # loop # and need to check/trigger events
                            time.sleep(0.01)
                            shutdown_countdown -= 1
                            if shutdown_countdown == 0:
                                self.shutdown_event.set()
                                self.stop()
                                break
                        else:
                            shutdown_countdown = 10
                    elif task is True:
                        # If received task is True
                        # and there is no active network threads then
                        # take some sleep
                        if not self.transport.get_active_threads_number():
                            time.sleep(0.01)
                    else:
                        logger_verbose.debug('Got new task from task'
                                             ' queue: %s', task)
                        task.network_try_count += 1 # pylint: disable=no-member
                        is_valid, reason = self.check_task_limits(task)
                        if is_valid:
                            task_grab = self.setup_grab_for_task(task)
                            if self.cache_pipeline:
                                # CACHE:
                                self.cache_pipeline.add_task(
                                    ('load', (task, task_grab)),
                                )
                                #print('!sent to cache')
                            else:
                                self.submit_task_to_transport(task, task_grab)
                        else:
                            self.log_rejected_task(task, reason)
                            # pylint: disable=no-member
                            handler = task.get_fallback_handler(self)
                            # pylint: enable=no-member
                            if handler:
                                handler(task)

                with self.timer.log_time('network_transport'):
                    logger_verbose.debug('Asking transport layer to do '
                                         'something')
                    self.transport.process_handlers()

                logger_verbose.debug('Processing network results (if any).')

                # Collect completed network results
                # Each result could be valid or failed
                # Result is dict {ok, grab, grab_config_backup, task, emsg}
                results = [(x, False) for x in
                           self.transport.iterate_results()]
                #print('!network results: %s' % results)
                if self.cache_pipeline:
                    # CACHE: for action, result in
                    # self.cache_pipeline.get_ready_results()
                    for action, result in (self.cache_pipeline
                                           .get_ready_results()):
                        #print('thing from cache: %s:%s' % (action, result))
                        assert action in ('network_result', 'task')
                        if action == 'network_result':
                            results.append((result, True))
                        elif action == 'task':
                            task = result
                            task_grab = self.setup_grab_for_task(task)
                            if (self.transport.get_free_threads_number()
                                    and (self.network_result_queue.qsize()
                                         < network_result_queue_limit)):
                                self.submit_task_to_transport(task, task_grab)
                            else:
                                pending_tasks.append(task)

                # Take sleep to avoid millions of iterations per second.
                # 1) If no results from network transport
                # 2) If task queue is empty or if there are only delayed tasks
                # 3) If no network activity
                # 4) If parser result queue is empty
                if (not results
                        and (task is None or bool(task) is True)
                        and not self.transport.get_active_threads_number()
                        and not self.parser_result_queue.qsize()
                        and (self.cache_pipeline is None
                             or self.cache_pipeline.is_idle())):
                        # CACHE: is_idle()
                        #or (self.cache_pipeline.input_queue.qsize() == 0
                        #    and self.cache_pipeline.is_idle()
                        #    and self.cache_pipeline.result_queue.qsize()
                        #    == 0))
                    time.sleep(0.001)

                for result, from_cache in results:
                    #print('!processing result %s' % result)
                    if self.cache_pipeline and not from_cache:
                        if result['ok']:
                            # CACHE:
                            self.cache_pipeline.add_task(
                                ('save', (result['task'], result['grab'])),
                            )
                    self.log_network_result_stats(
                        result, from_cache=from_cache)

                    is_valid = False
                    if result['task'].get('raw'):
                        is_valid = True
                    elif result['ok']:
                        res_code = result['grab'].doc.code
                        if self.is_valid_network_response_code(
                                res_code, result['task']):
                            is_valid = True

                    if is_valid:
                        self.network_result_queue.put(result)
                    else:
                        self.log_failed_network_result(result)
                        # Try to do network request one more time
                        # TODO:
                        # Implement valid_try_limit
                        # Use it if request failed not because of network error
                        # But because of content integrity check
                        if self.network_try_limit > 0:
                            result['task'].refresh_cache = True
                            result['task'].setup_grab_config(
                                result['grab_config_backup'])
                            self.add_task(result['task'])
                    if from_cache:
                        self.stat.inc('spider:task-%s-cache'
                                      % result['task'].name)
                    self.stat.inc('spider:request')

                while True:
                    try:
                        p_res, p_task = (self.parser_result_queue
                                         .get(block=False))
                    except queue.Empty:
                        break
                    else:
                        self.stat.inc('spider:parser-result')
                        self.process_handler_result(p_res, p_task)

                if not self.shutdown_event.is_set():
                    self.parser_pipeline.check_pool_health()

            logger_verbose.debug('Work done')
        except KeyboardInterrupt:
            logger.info('\nGot ^C signal in process %d. Stopping.',
                        os.getpid())
            self.interrupted = True
            raise
        finally:
            # This code is executed when main cycles is breaked
            self.timer.stop('total')
            self.stat.print_progress_line()
            self.shutdown()

            # Stop HTTP API process
            if http_api_proc:
                http_api_proc.server.shutdown()
                http_api_proc.join()

            if self.task_queue:
                self.task_queue.clear()

            # Stop parser processes
            self.shutdown_event.set()
            self.parser_pipeline.shutdown()
            logger.debug('Main process [pid=%s]: work done', os.getpid())

    def log_failed_network_result(self, res):
        # Log the error
        if res['ok']:
            msg = 'http-%s' % res['grab'].doc.code
        else:
            msg = res['error_abbr']

        self.stat.inc('error:%s' % msg)
        # logger.error(u'Network error: %s' % msg)#%
        # make_unicode(msg, errors='ignore'))

    def log_rejected_task(self, task, reason):
        logger_verbose.debug('Task %s is rejected due to %s limit',
                             task.name, reason)
        if reason == 'task-try-count':
            self.stat.collect('task-count-rejected',
                              task.url)
        elif reason == 'network-try-count':
            self.stat.collect('network-count-rejected',
                              task.url)
        else:
            raise SpiderError('Unknown response from '
                              'check_task_limits: %s'
                              % reason)

    def process_handler_result(self, result, task):
        """
        Process result received from the task handler.

        Result could be:
        * None
        * Task instance
        * Data instance.
        * dict:
          * {type: "stat", counters: [], collections: []}
        * ResponseNotValid-based exception
        * Arbitrary exception
        """

        if isinstance(result, Task):
            self.add_task(result)
        elif isinstance(result, Data):
            handler = self.find_data_handler(result)
            try:
                data_result = handler(**result.storage)
                if data_result is None:
                    pass
                else:
                    for something in data_result:
                        self.process_handler_result(something, task)

            except Exception as ex: # pylint: disable=broad-except
                self.process_handler_error('data_%s' % result.handler_key,
                                           ex, task)
        elif result is None:
            pass
        elif isinstance(result, ResponseNotValid):
            self.add_task(task.clone(refresh_cache=True))
            error_code = result.__class__.__name__.replace('_', '-')
            self.stat.inc('integrity:%s' % error_code)
        elif isinstance(result, Exception):
            handler = self.find_task_handler(task)
            handler_name = getattr(handler, '__name__', 'NONE')
            self.process_handler_error(handler_name, result, task)
        elif isinstance(result, dict):
            if result.get('type') == 'stat':
                for name, count in result['counters'].items():
                    self.stat.inc(name, count)
                for name, items in result['collections'].items():
                    for item in items:
                        self.stat.collect(name, item)
            else:
                raise SpiderError('Unknown result type: %s' % result)
        else:
            raise SpiderError('Unknown result type: %s' % result)
