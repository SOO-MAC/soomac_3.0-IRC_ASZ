#!/usr/bin/env python3

import copy
import json
import threading

from http.server import (
    SimpleHTTPRequestHandler,
    ThreadingHTTPServer,
)

from pathlib import Path
from urllib.parse import urlparse


UI_DIR = Path(__file__).resolve().parent / "ui"


class CustomerUIStore:

    def __init__(self):

        self.lock = threading.Lock()

        self.revision = 0

        self.voice_mode = "standby"

        self.last_heard = ""

        self.conversation = []

        self.items = []

        self.order_id = None
        self.mobile_order_id = None
        self.total_price = None
        self.order_mode = None


    def _touch(self):

        self.revision += 1


    def snapshot(self):

        with self.lock:

            return {
                "revision": self.revision,

                "voice_mode":
                    self.voice_mode,

                "last_heard":
                    self.last_heard,

                "conversation":
                    copy.deepcopy(
                        self.conversation
                    ),

                "items":
                    copy.deepcopy(
                        self.items
                    ),

                "order_id":
                    self.order_id,

                "mobile_order_id":
                    self.mobile_order_id,

                "total_price":
                    self.total_price,

                "order_mode":
                    self.order_mode,
            }


    def reset_for_vehicle(self):

        with self.lock:

            self.voice_mode = (
                "listening"
            )

            self.last_heard = ""

            self.conversation = []

            self.items = []

            self.order_id = None
            self.mobile_order_id = None
            self.total_price = None
            self.order_mode = None

            self._touch()


    def set_voice_mode(
        self,
        mode,
    ):

        with self.lock:

            self.voice_mode = str(
                mode or "standby"
            )

            self._touch()


    def add_customer(
        self,
        text,
    ):

        text = str(
            text or ""
        ).strip()

        if not text:
            return

        with self.lock:

            self.last_heard = text

            self.conversation.append({
                "role": "customer",
                "text": text,
            })

            self._touch()


    def add_staff(
        self,
        text,
    ):

        text = str(
            text or ""
        ).strip()

        if not text:
            return

        with self.lock:

            self.conversation.append({
                "role": "staff",
                "text": text,
            })

            self._touch()


    def set_items(
        self,
        items,
    ):

        with self.lock:

            self.items = copy.deepcopy(
                list(items or [])
            )

            self._touch()


    def set_order_meta(
        self,
        *,
        order_id=None,
        mobile_order_id=None,
        total_price=None,
        order_mode=None,
    ):

        with self.lock:

            self.order_id = order_id
            self.mobile_order_id = mobile_order_id
            self.total_price = total_price
            self.order_mode = order_mode

            self._touch()


STORE = CustomerUIStore()


class CustomerUIHandler(
    SimpleHTTPRequestHandler
):

    def __init__(
        self,
        *args,
        **kwargs,
    ):

        super().__init__(
            *args,
            directory=str(UI_DIR),
            **kwargs,
        )


    def log_message(
        self,
        format,
        *args,
    ):
        # 브라우저 polling 로그로
        # 터미널이 도배되는 것을 막는다.
        return


    def _send_json(
        self,
        data,
        status=200,
    ):

        raw = json.dumps(
            data,
            ensure_ascii=False,
        ).encode("utf-8")

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )

        self.send_header(
            "Content-Length",
            str(len(raw)),
        )

        self.send_header(
            "Cache-Control",
            "no-store",
        )

        self.end_headers()

        self.wfile.write(raw)


    def do_GET(self):

        path = urlparse(
            self.path
        ).path


        if path == "/api/state":

            self._send_json(
                STORE.snapshot()
            )

            return


        if path == "/":

            self.path = (
                "/customer.html"
            )


        return super().do_GET()


_server = None
_server_thread = None


def start_customer_ui_server(
    host="127.0.0.1",
    port=8080,
):

    global _server
    global _server_thread


    if _server is not None:
        return


    _server = ThreadingHTTPServer(
        (host, port),
        CustomerUIHandler,
    )


    _server_thread = threading.Thread(
        target=_server.serve_forever,
        name="customer-ui-server",
        daemon=True,
    )

    _server_thread.start()


    print()
    print(
        "[UI] Customer UI : "
        f"http://{host}:{port}/customer.html"
    )


def stop_customer_ui_server():

    global _server
    global _server_thread


    if _server is None:
        return


    _server.shutdown()

    _server.server_close()

    _server = None
    _server_thread = None


def ui_reset_for_vehicle():

    STORE.reset_for_vehicle()


def ui_add_customer_message(
    text,
):

    STORE.add_customer(
        text
    )


def ui_add_staff_message(
    text,
):

    STORE.add_staff(
        text
    )


def ui_set_voice_mode(
    mode,
):

    STORE.set_voice_mode(
        mode
    )


def ui_set_order_items(
    items,
):

    STORE.set_items(
        items
    )


def ui_set_order_meta(
    *,
    order_id=None,
    mobile_order_id=None,
    total_price=None,
    order_mode=None,
):

    STORE.set_order_meta(
        order_id=order_id,
        mobile_order_id=mobile_order_id,
        total_price=total_price,
        order_mode=order_mode,
    )
