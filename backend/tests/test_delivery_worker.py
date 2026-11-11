from threading import Event

import pytest

from aftercare import delivery_worker as module


class Journal:
    lease_seconds = .03

    def __init__(self):
        self.valid = True
        self.renewed = Event()
        self.finished = []
        self.owner = 'owner'

    def claim(self, job):
        return self.owner

    def owned(self, job, owner, *, session=None):
        return self.valid

    def renew(self, job, owner):
        self.renewed.set()
        return self.valid

    def finish(self, job, owner, **options):
        self.finished.append(options)


def test_long_operation_renews_and_defers_still_active_target():
    journal = Journal()
    def operation(admitted):
        assert journal.renewed.wait(1)
        assert admitted()
    assert module.run_claimed(journal, 'job', operation, lambda: True)
    assert journal.finished == [{'retry': True}]


def test_lost_owner_cannot_finish_or_admit_next_call():
    journal = Journal()
    def operation(admitted):
        journal.valid = False
        assert not admitted()
    module.run_claimed(journal, 'job', operation, lambda: False)
    assert journal.finished == []


def test_failure_recorded_and_propagated():
    journal = Journal()
    def operation(admitted):
        raise ValueError('worker failed')
    with pytest.raises(ValueError, match='worker failed'):
        module.run_claimed(journal, 'job', operation, lambda: False)
    assert journal.finished == [{'failed': True}]


def test_duplicate_delivery_does_not_execute():
    journal = Journal()
    journal.owner = None
    assert not module.run_claimed(journal, 'job', lambda _: pytest.fail('duplicate'), lambda: False)
    assert journal.finished == []
