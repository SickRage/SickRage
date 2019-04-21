# Author: echel0n <echel0n@sickrage.ca>
# URL: https://sickrage.ca
# Git: https://git.sickrage.ca/SiCKRAGE/sickrage.git
#
# This file is part of SiCKRAGE.
#
# SiCKRAGE is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SiCKRAGE is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with SiCKRAGE.  If not, see <http://www.gnu.org/licenses/>.


import datetime
import threading
import time

from tornado import gen
from tornado.queues import Queue, PriorityQueue

import sickrage


class srQueuePriorities(object):
    EXTREME = 5
    HIGH = 10
    NORMAL = 20
    LOW = 30
    PAUSED = 99


class srQueue(object):
    def __init__(self, name="QUEUE"):
        super(srQueue, self).__init__()
        self.name = name
        self.queue = PriorityQueue()
        self._result_queue = Queue()
        self.processing = []
        self.min_priority = srQueuePriorities.EXTREME
        self.amActive = False
        self.stop = False

    async def watch(self):
        """
        Process items in this queue
        """

        self.amActive = True

        while not (self.stop and self.queue.empty()):
            if not self.is_paused:
                await sickrage.app.io_loop.run_in_executor(None, self.worker, await self.get())

            await gen.sleep(1)

        self.amActive = False

    def worker(self, item):
        threading.currentThread().setName(item.name)
        try:
            self.processing.append(item)
            item.run()
        finally:
            self.processing.remove(item)
            self.queue.task_done()

    def get(self):
        return self.queue.get()

    def put(self, item, *args, **kwargs):
        """
        Adds an item to this queue

        :param item: Queue object to add
        :return: item
        """
        if self.stop:
            return

        item.added = datetime.datetime.now()
        item.name = "{}-{}".format(self.name, item.name)
        item.result_queue = self._result_queue
        self.queue.put(item)

        return item

    @property
    def queue_items(self):
        return self.queue._queue + self.processing

    @property
    def is_busy(self):
        return bool(len(self.queue_items))

    @property
    def is_paused(self):
        return self.min_priority == srQueuePriorities.PAUSED

    def pause(self):
        """Pauses this queue"""
        sickrage.app.log.info("Pausing {}".format(self.name))
        self.min_priority = srQueuePriorities.PAUSED

    def unpause(self):
        """Unpauses this queue"""
        sickrage.app.log.info("Un-pausing {}".format(self.name))
        self.min_priority = srQueuePriorities.EXTREME


class srQueueItem(object):
    def __init__(self, name, action_id=0):
        super(srQueueItem, self).__init__()
        self.name = name.replace(" ", "-").upper()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.action_id = action_id
        self.added = None
        self.result = None
        self.result_queue = None
        self.priority = srQueuePriorities.NORMAL
        self.is_alive = False

    def __eq__(self, other):
        return self.priority == other.priority

    def __ne__(self, other):
        return not self.priority == other.priority

    def __lt__(self, other):
        return self.priority < other.priority
