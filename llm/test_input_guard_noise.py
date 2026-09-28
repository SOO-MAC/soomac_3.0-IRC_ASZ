from drive_thru_app import guard_customer_input


def guard(text, pending=None, mobile=False):
    return guard_customer_input(
        text,
        pending=pending,
        mobile_confirmation_pending=mobile,
    )


# 정상 주문
valid = [
    "불고기버거 하나 주세요",
    "치즈버거 세트로 주세요",
    "감자튀김 말고 치즈스틱으로 바꿔주세요",
    "피클 빼주세요",
    "하나 더 주세요",
    "주문 마무리할게요",
    "427번이요",
]

for text in valid:
    result = guard(text)

    assert result["allow"], (
        f"정상 입력이 막힘: {text} -> {result}"
    )


# pending 짧은 응답
pending_cases = [
    ("단품이요", (1, "type")),
    ("콜라요", (1, "drink")),
    ("라지요", (1, "drink_size")),
    ("치즈스틱이요", (1, "side")),
]

for text, pending in pending_cases:
    result = guard(text, pending=pending)

    assert result["allow"], (
        f"pending 입력이 막힘: {text} -> {result}"
    )


# 엉뚱한 STT / 잡음성 transcript
reject = [
    "오늘 날씨 어때",
    "지금 몇 시야",
    "음악 틀어줘",
    "너 이름이 뭐야",
    "안녕하세요 반갑습니다",
    "감사합니다",
    "시청해주셔서 감사합니다",
    "구독과 좋아요 부탁드립니다",
]

for text in reject:
    result = guard(text)

    assert not result["allow"], (
        f"차단 실패: {text} -> {result}"
    )


print("PASS 2A: normal orders allowed")
print("PASS 2B: pending answers allowed")
print("PASS 2C: out-of-domain STT rejected")
print()
print("INPUT DOMAIN GUARD: ALL PASS")
