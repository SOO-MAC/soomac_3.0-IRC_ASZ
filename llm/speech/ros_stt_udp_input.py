#!/usr/bin/env python3

import socket

from stt_guard import STTResult


class RosSTTUDPInput:
    """
    ROS2 Python 3.10 bridge가 보내는 STT 문자열을
    Python LLM 앱에서 UDP로 받는다.
    """

    def __init__(
        self,
        host="127.0.0.1",
        port=5006,
        timeout=0.25,
    ):
        self.sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )

        self.sock.bind(
            (host, port)
        )

        self.sock.settimeout(
            timeout
        )

        self._closed = False

        print(
            f"[STT INPUT] UDP listening "
            f"{host}:{port}"
        )

    def receive_once(self):

        if self._closed:
            return STTResult(
                ok=False,
                error_code="STT_INPUT_CLOSED",
                error_detail="UDP STT input is closed.",
            )

        try:
            data, _ = self.sock.recvfrom(
                65535
            )

        except socket.timeout:
            return STTResult(
                transcript=None,
                ok=True,
                is_final=False,
            )

        except OSError as e:
            return STTResult(
                ok=False,
                error_code=type(e).__name__,
                error_detail=str(e),
            )

        try:
            text = (
                data.decode(
                    "utf-8",
                    errors="strict",
                )
                .strip()
            )

        except UnicodeDecodeError as e:
            return STTResult(
                ok=False,
                error_code="INVALID_UTF8",
                error_detail=str(e),
            )

        return STTResult(
            transcript=text,
            ok=True,
            is_final=True,
            confidence=None,
        )

    def drain(self):
        """
        새 고객 세션 시작 전에 socket에 쌓여 있던
        이전 고객 발화를 전부 버린다.
        """

        if self._closed:
            return

        old_timeout = self.sock.gettimeout()

        try:
            self.sock.setblocking(False)

            while True:
                try:
                    self.sock.recvfrom(65535)

                except BlockingIOError:
                    break

                except OSError:
                    break

        finally:
            self.sock.settimeout(old_timeout)

    def close(self):

        if self._closed:
            return

        self._closed = True

        try:
            self.sock.close()

        except Exception:
            pass
