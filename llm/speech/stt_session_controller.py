class STTSessionController:
    """
    차량 주문 세션과 STT 수명주기를 묶는다.

    - 주문 중이 아닐 때 STT 결과를 받지 않는다.
    - 새 주문 시작 전에 이전 고객 queue/buffer를 비운다.
    - 주문 종료 즉시 STT를 막고 남은 데이터를 폐기한다.
    """

    def __init__(
        self,
        speech_worker,
        *,
        input_source=None,
        remote_gate=None,
    ):
        self.speech_worker = speech_worker
        self.input_source = input_source
        self.remote_gate = remote_gate
        self._active = False

    @property
    def active(self):
        return self._active

    def _set_remote_gate(self, enabled):
        if self.remote_gate is None:
            return

        self.remote_gate(bool(enabled))

    def _drain_input(self):
        if self.input_source is None:
            return

        drain = getattr(
            self.input_source,
            "drain",
            None,
        )

        if callable(drain):
            drain()

    def start(self):

        self._set_remote_gate(False)

        self.speech_worker.disable()
        self.speech_worker.clear()

        self._drain_input()

        self.speech_worker.enable()

        self._set_remote_gate(True)

        self._active = True

    def stop(self):

        self._set_remote_gate(False)

        self.speech_worker.disable()
        self.speech_worker.clear()

        self._drain_input()

        self._active = False

    def reset(self):

        self.stop()
        self.start()
