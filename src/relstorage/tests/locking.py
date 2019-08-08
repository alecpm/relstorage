# -*- coding: utf-8 -*-
##############################################################################
#
# Copyright (c) 2019 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
"""
Test mixin dealing with different locking scenarios.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import threading
import time

from functools import partial
from functools import update_wrapper

import transaction

from ZODB.DB import DB
from ZODB.Connection import TransactionMetaData

from ZODB.tests.MinPO import MinPO

from . import TestCase

def WithAndWithoutInterleaving(func):
    # Expands a test case into two tests, for those that can run
    # both with the stored procs and without it.
    def _interleaved(self):
        adapter = self._storage._adapter
        if not adapter.DEFAULT_LOCK_OBJECTS_AND_DETECT_CONFLICTS_INTERLEAVABLE:
            adapter.force_lock_objects_and_detect_conflicts_interleavable = True

        func(self)
        if 'force_lock_objects_and_detect_conflicts_interleavable' in adapter.__dict__:
            del adapter.force_lock_objects_and_detect_conflicts_interleavable

    def _stored_proc(self):
        adapter = self._storage._adapter
        if adapter.DEFAULT_LOCK_OBJECTS_AND_DETECT_CONFLICTS_INTERLEAVABLE:
            # No stored proc version.
            return
        func(self)

    def test(self):
        # Sadly using self.subTest()
        # causes zope-testrunner to lose the exception reports.
        _stored_proc(self)
        _interleaved(self)

    return update_wrapper(test, func)


class TestLocking(TestCase):
    # pylint:disable=abstract-method

    _storage = None

    def make_storage(self, *args, **kwargs):
        raise NotImplementedError


    def __store_two_for_read_current_error(self):
        db = self._closing(DB(self._storage))
        conn = db.open()
        root = conn.root()
        root['object1'] = MinPO('object1')
        root['object2'] = MinPO('object2')
        transaction.commit()

        obj1_oid = root['object1']._p_oid
        obj2_oid = root['object2']._p_oid
        obj1_tid = root['object1']._p_serial
        assert obj1_tid == root['object2']._p_serial

        conn.close()
        # We can't close the DB, that will close the storage that we
        # still need.
        return obj1_oid, obj2_oid, obj1_tid

    def __read_current_and_lock(self, storage, read_current_oid, lock_oid, tid):
        tx = TransactionMetaData()
        storage.tpc_begin(tx)
        storage.checkCurrentSerialInTransaction(read_current_oid, tid, tx)
        storage.store(lock_oid, tid, b'bad pickle2', '', tx)
        storage.tpc_vote(tx)
        return tx

    def __do_check_error_with_conflicting_concurrent_read_current(
            self,
            exception_in_b,
            commit_lock_timeout=None,
            storageA=None,
            storageB=None,
            identical_pattern_a_b=False,
            copy_interleave=('A', 'B'),
            abort=True
    ):
        root_adapter = self._storage._adapter
        if commit_lock_timeout:
            root_adapter.locker.commit_lock_timeout = commit_lock_timeout
            self._storage._options.commit_lock_timeout = commit_lock_timeout

        if storageA is None:
            storageA = self._closing(self._storage.new_instance())
        if storageB is None:
            storageB = self._closing(self._storage.new_instance())

        should_ileave = root_adapter.force_lock_objects_and_detect_conflicts_interleavable
        if 'A' in copy_interleave:
            storageA._adapter.force_lock_objects_and_detect_conflicts_interleavable = should_ileave
        if 'B' in copy_interleave:
            storageB._adapter.force_lock_objects_and_detect_conflicts_interleavable = should_ileave

        # First, store the two objects in an accessible location.
        obj1_oid, obj2_oid, tid = self.__store_two_for_read_current_error()

        # Now transaction A readCurrent 1 and modify 2
        # up through the vote phase
        txa = self.__read_current_and_lock(storageA, obj1_oid, obj2_oid, tid)

        if not identical_pattern_a_b:
            # Second transaction does exactly the opposite, and blocks,
            # raising an exception (usually)
            lock_b = partial(
                self.__read_current_and_lock,
                storageB,
                obj2_oid,
                obj1_oid,
                tid
            )
        else:
            lock_b = partial(
                self.__read_current_and_lock,
                storageB,
                obj1_oid,
                obj2_oid,
                tid
            )

        txb = None
        before = time.time()
        if exception_in_b:
            with self.assertRaises(exception_in_b):
                lock_b()
        else:
            txb = lock_b()
        after = time.time()
        duration_blocking = after - before

        if abort:
            storageA.tpc_abort(txa)
            storageB.tpc_abort(txb)

        return duration_blocking

    @WithAndWithoutInterleaving
    def checkTL_ConflictingReadCurrent(self):
        # Given two objects 1 and 2, if transaction A does readCurrent(1)
        # and modifies 2 up through the voting phase, and then transaction
        # B does precisely the same thing,
        # we get an error after ``commit_lock_timeout``  and not a deadlock.
        from relstorage.adapters.interfaces import UnableToLockRowsToModifyError

        # Use a very small commit lock timeout.
        commit_lock_timeout = 0.1
        duration_blocking = self.__do_check_error_with_conflicting_concurrent_read_current(
            UnableToLockRowsToModifyError,
            commit_lock_timeout=commit_lock_timeout,
            identical_pattern_a_b=True
        )

        self.assertLessEqual(duration_blocking, commit_lock_timeout * 3)

    @WithAndWithoutInterleaving
    def checkTL_OverlappedReadCurrent_SharedLocksFirst(self):
        # Starting with two objects 1 and 2, if transaction A modifies 1 and
        # does readCurrent of 2, up through the voting phase, and transaction B does
        # exactly the opposite, transaction B is immediately killed with a read conflict
        # error. (We use the same two objects instead of a new object in transaction B to prove
        # shared locks are taken first.)
        from relstorage.adapters.interfaces import UnableToLockRowsToReadCurrentError
        commit_lock_timeout = 1
        duration_blocking = self.__do_check_error_with_conflicting_concurrent_read_current(
            UnableToLockRowsToReadCurrentError,
            commit_lock_timeout=commit_lock_timeout,
        )
        # The NOWAIT lock should be very quick to fire.
        if self._storage._adapter.locker.supports_row_lock_nowait:
            self.assertLessEqual(duration_blocking, commit_lock_timeout)
        else:
            # Sigh. Old MySQL. Very slow. This takes around 4.5s to run both iterations.
            self.assertLessEqual(duration_blocking, commit_lock_timeout * 2.5)


    def __lock_rows_being_modified_only(self, storage, cursor, _current_oids, _share_blocks):
        # A monkey-patch for Locker.lock_current_objects to only take the exclusive
        # locks.
        storage._adapter.locker._lock_rows_being_modified(cursor)

    def __assert_small_blocking_duration(self, storage, duration_blocking):
        # Even though we just went with the default commit_lock_timeout,
        # which is large...
        self.assertGreaterEqual(storage._options.commit_lock_timeout, 10)
        # ...the lock violation happened very quickly
        self.assertLessEqual(duration_blocking, 3)

    def checkTL_InterleavedConflictingReadCurrent(self):
        # Similar to
        # ``checkTL_ConflictingReadCurrent``
        # except that we pause the process immediately after txA takes
        # its exclusive locks to let txB take *its* exclusive locks.
        # Then txB can go for the shared locks, which will block if
        # we're not in ``NOWAIT`` mode for shared locks. This tests
        # that we don't block, we report a retryable error. Note that
        # we don't adjust the commit_lock_timeout; it doesn't apply.
        #
        # If we're using a stored procedure, this test will break
        # because we won't be able to force the interleaving, so we make it
        # use the old version.

        from relstorage.adapters.interfaces import UnableToLockRowsToReadCurrentError
        storageA = self._closing(self._storage.new_instance())
        # This turns off stored procs and lets us control that phase.
        storageA._adapter.force_lock_objects_and_detect_conflicts_interleavable = True

        storageA._adapter.locker.lock_current_objects = partial(
            self.__lock_rows_being_modified_only,
            storageA)

        duration_blocking = self.__do_check_error_with_conflicting_concurrent_read_current(
            UnableToLockRowsToReadCurrentError,
            storageA=storageA,
            copy_interleave=()
        )

        self.__assert_small_blocking_duration(storageA, duration_blocking)

    def checkTL_InterleavedConflictingReadCurrentDeadlock(self):
        # Like
        # ``checkTL_InterleavedConflictingReadCurrent``
        # except that we interleave both txA and txB: txA takes modify
        # lock, txB takes modify lock, txA attempts shared lock, txB
        # attempts shared lock. This results in a database deadlock, which is reported as
        # a retryable error.
        #
        # We have to use a thread to do the shared locks because it blocks.
        from relstorage.adapters.interfaces import UnableToLockRowsToReadCurrentError

        storageA = self._closing(self._storage.new_instance())
        storageB = self._closing(self._storage.new_instance())
        storageA.last_error = storageB.last_error = None

        storageA._adapter.locker.lock_current_objects = partial(
            self.__lock_rows_being_modified_only,
            storageA)
        storageB._adapter.locker.lock_current_objects = partial(
            self.__lock_rows_being_modified_only,
            storageB)

        # This turns off stored procs for locking.
        storageA._adapter.force_lock_readCurrent_for_share_blocking = True
        storageB._adapter.force_lock_readCurrent_for_share_blocking = True

        # This won't actually block, we haven't conflicted yet.
        self.__do_check_error_with_conflicting_concurrent_read_current(
            None,
            storageA=storageA,
            storageB=storageB,
            abort=False
        )

        cond = threading.Condition()
        cond.acquire()
        def lock_shared(storage, notify=True):
            cursor = storage._store_connection.cursor
            read_current_oids = storage._tpc_phase.required_tids.keys()
            if notify:
                cond.acquire(5)
                cond.notifyAll()
                cond.release()

            try:
                storage._adapter.locker._lock_readCurrent_oids_for_share(
                    cursor,
                    read_current_oids,
                    True
                )
            except UnableToLockRowsToReadCurrentError as ex:
                storage.last_error = ex
            finally:
                if notify:
                    cond.acquire(5)
                    cond.notifyAll()
                    cond.release()


        thread_locking_a = threading.Thread(
            target=lock_shared,
            args=(storageA,)
        )
        thread_locking_a.start()

        # wait for the background thread to get ready to lock.
        cond.acquire(5)
        cond.wait(5)
        cond.release()

        begin = time.time()

        lock_shared(storageB, notify=False)

        # Wait for background thread to finish.
        cond.acquire(5)
        cond.wait(5)
        cond.release()

        end = time.time()
        duration_blocking = end - begin

        # Now, one or the other storage got killed by the deadlock
        # detector, but not both. Which one depends on the database.
        # PostgreSQL likes to kill the foreground thread (storageB),
        # MySQL likes to kill the background thread (storageA)
        self.assertTrue(storageA.last_error or storageB.last_error)
        self.assertFalse(storageA.last_error and storageB.last_error)

        last_error = storageA.last_error or storageB.last_error

        self.assertIn('deadlock', str(last_error).lower())

        self.__assert_small_blocking_duration(storageA, duration_blocking)
        self.__assert_small_blocking_duration(storageB, duration_blocking)
