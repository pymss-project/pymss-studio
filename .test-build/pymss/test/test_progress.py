from pymss.utils import _ProgressContext


class FakeProgressBar:
    def __init__(self):
        self.updates = []

    def update(self, amount):
        self.updates.append(amount)


def test_progress_emit_updates_bar_from_absolute_done():
    events = []
    progress = _ProgressContext(pbar=False, total=100, callback=lambda *args: events.append(args))
    progress.bar = FakeProgressBar()

    progress.emit(10)
    progress.emit(25)
    progress.emit(100)

    assert progress.bar.updates == [10, 15, 75]
    assert events == [
        (0, 100, "Processing audio"),
        (10, 100, "Processing audio"),
        (25, 100, "Processing audio"),
        (100, 100, "Processing audio"),
    ]


def test_progress_update_reuses_absolute_emit_path():
    events = []
    progress = _ProgressContext(pbar=False, total=100, callback=lambda *args: events.append(args))
    progress.bar = FakeProgressBar()

    progress.update(30)
    progress.update(80)

    assert progress.bar.updates == [30, 70]
    assert events[-1] == (100, 100, "Processing audio")
