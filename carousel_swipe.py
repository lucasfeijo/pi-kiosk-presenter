"""Interactive horizontal paging, independent of camera playback."""
import time


class SwipeGesture:
    def __init__(self, root, width, draw, finish, clock=time.monotonic):
        self.root, self.width, self.draw, self.finish = root, width, draw, finish
        self.clock = clock
        self.start = None
        self.axis = None
        self.offset = 0
        self.samples = []
        self.after = None
        self.serial = 0

    @property
    def tracking(self):
        return self.start is not None

    @property
    def busy(self):
        return self.tracking or self.after is not None

    def begin(self, x, y):
        self.cancel()
        self.start = (x, y)
        self.axis = None
        self.samples = [(self.clock(), x)]

    def move(self, x, y):
        if not self.tracking:
            return
        dx, dy = x - self.start[0], y - self.start[1]
        if self.axis is None and max(abs(dx), abs(dy)) >= 12:
            self.axis = 'horizontal' if abs(dx) > abs(dy) * 1.2 else 'vertical'
        now = self.clock()
        self.samples = [(t, px) for t, px in self.samples if now - t <= .12]
        self.samples.append((now, x))
        if self.axis == 'horizontal':
            width = max(1, self.width())
            self.offset = max(-width, min(width, dx))
            self.draw(self.offset)

    def release(self, x, y):
        if not self.tracking:
            return
        self.move(x, y)
        self.start = None
        if self.axis != 'horizontal':
            self.finish(0)
            return
        velocity = 0
        if len(self.samples) > 1:
            t0, x0 = self.samples[0]
            t1, x1 = self.samples[-1]
            if t1 > t0:
                velocity = (x1 - x0) / (t1 - t0)
        commit = (abs(self.offset) >= self.width() * .25 or
                  (abs(self.offset) >= 25 and abs(velocity) >= 600
                   and velocity * self.offset > 0))
        delta = (1 if self.offset < 0 else -1) if commit else 0
        origin = self.offset
        destination = -delta * self.width()
        started = self.clock()
        self.serial += 1
        serial = self.serial

        def frame():
            if serial != self.serial:
                return
            progress = min(1, (self.clock() - started) / .20)
            self.offset = origin + (destination - origin) * (1 - (1 - progress) ** 3)
            self.draw(self.offset)
            if progress < 1:
                self.after = self.root.after(16, frame)
            else:
                self.after = None
                self.offset = 0
                self.finish(delta)
        self.after = self.root.after(0, frame)

    def cancel(self):
        active = self.busy
        self.serial += 1
        if self.after is not None:
            self.root.after_cancel(self.after)
        self.after = None
        self.start = None
        self.offset = 0
        if active:
            self.finish(0)
