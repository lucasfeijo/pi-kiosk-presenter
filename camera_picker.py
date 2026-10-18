"""A pane-local camera chooser with touch scrolling and an inactivity timeout."""


class CameraPicker:
    def __init__(self, tk, root, on_close):
        self.tk, self.root, self.on_close = tk, root, on_close
        self.window = None
        self.after = None
        self.press = None
        self.dragged = False

    @property
    def is_open(self):
        return self.window is not None

    def show(self, title, names):
        if self.is_open:
            return
        tk = self.tk
        self.window = window = tk.Toplevel(self.root)
        window.withdraw()
        window.title(f"carousel-picker-{self.root.winfo_id()}")
        window.overrideredirect(True)
        window.transient(self.root)
        background = title.cget("background")
        window.configure(background=background)
        self.canvas = tk.Canvas(window, background=background, borderwidth=0,
                                highlightthickness=0, yscrollincrement=1)
        self.canvas.pack(fill="both", expand=True)
        self.content = tk.Frame(self.canvas, background=background)
        self.content_id = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self.rows = []
        for name in names:
            row = tk.Label(self.content, text=name, font=title.cget("font"),
                           background=background, foreground=title.cget("foreground"),
                           borderwidth=0, padx=title.cget("padx"), pady=title.cget("pady"))
            row.pack(fill="x")
            self.rows.append(row)
        window.bind("<ButtonPress-1>", self._press)
        window.bind("<B1-Motion>", self._move)
        window.bind("<ButtonRelease-1>", self._release)
        window.bind("<Button-4>", lambda e: self._scroll(-40))
        window.bind("<Button-5>", lambda e: self._scroll(40))
        window.bind("<MouseWheel>", lambda e: self._scroll(-40 if e.delta > 0 else 40))
        self.layout()
        window.deiconify()
        window.lift(self.root)
        window.grab_set()
        self._arm_timeout()

    def layout(self):
        if not self.is_open:
            return
        self.window.update_idletasks()
        max_width = max(1, int(self.root.winfo_width() * .9))
        width = min(max_width, max(row.winfo_reqwidth() for row in self.rows))
        for row in self.rows:
            row.configure(wraplength=max(1, width - 28))
        self.canvas.itemconfigure(self.content_id, width=width)
        self.window.update_idletasks()
        content_height = self.content.winfo_reqheight()
        height = min(max(1, int(self.root.winfo_height() * .8)), content_height)
        self.canvas.configure(scrollregion=(0, 0, width, content_height))
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        self.window.geometry(f"{width}x{height}{x:+d}{y:+d}")

    def _cancel_timeout(self):
        if self.after is not None:
            self.root.after_cancel(self.after)
            self.after = None

    def _arm_timeout(self):
        self._cancel_timeout()
        if self.is_open:
            self.after = self.root.after(5000, self.close)

    def _press(self, event):
        x, y = event.x_root, event.y_root
        w = self.window
        if not (w.winfo_rootx() <= x < w.winfo_rootx() + w.winfo_width()
                and w.winfo_rooty() <= y < w.winfo_rooty() + w.winfo_height()):
            self.close()
            return "break"
        self._arm_timeout()
        self.press = (x, y, event.widget)
        self.dragged = False
        self.scroll_start = self.canvas.yview()[0]
        return "break"

    def _move(self, event):
        if self.press is not None:
            dx, dy = event.x_root - self.press[0], event.y_root - self.press[1]
            self.dragged = self.dragged or max(abs(dx), abs(dy)) >= 12
            if self.dragged:
                self._arm_timeout()
                height = max(1, self.content.winfo_reqheight())
                self.canvas.yview_moveto(self.scroll_start - dy / height)
        return "break"

    def _release(self, event):
        if self.press is None:
            return "break"
        self._move(event)
        row = self.press[2]
        self.press = None
        if (not self.dragged and row in self.rows
                and row.winfo_rootx() <= event.x_root < row.winfo_rootx() + row.winfo_width()
                and row.winfo_rooty() <= event.y_root < row.winfo_rooty() + row.winfo_height()):
            self.close(self.rows.index(row))
        else:
            self._arm_timeout()
        return "break"

    def _scroll(self, distance):
        self.canvas.yview_scroll(distance, "units")
        self._arm_timeout()
        return "break"

    def close(self, index=None, notify=True):
        self._cancel_timeout()
        if not self.is_open:
            return
        window, self.window = self.window, None
        self.press = None
        window.grab_release()
        window.destroy()
        if notify:
            self.on_close(index)
