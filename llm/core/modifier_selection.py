"""Pure burger modifier parsing. No model calls or order-state mutation."""

import re


def compact(text):
    return re.sub(r"[\s!?.,~]+", "", str(text or "").lower())


def detect_request(text, toppings, excludes, menus, sides):
    """Separate an imperative from an existing-option reference.

    Return selector text with the operation phrase removed, so an add request
    cannot accidentally require that same topping to already be present.
    """
    value = compact(text)
    # Inline modifiers on a newly ordered burger belong to the existing
    # add-order pipeline, not modifications of an already ordered burger.
    if (any(compact(label) in value for label in menus.values())
            and re.search(r"(?:하나|한개|두개|세개|\d+개)", value)
            and re.search(r"(?:추가해서|빼서|제외해서|없이).*주세요", value)):
        return None
    if re.search(r"얼마|가격|칼로리|종류|가능|있나요|있어요|뭐가|무엇", value):
        return None
    masked = value
    for label in sorted([*menus.values(), *sides.values()], key=len, reverse=True):
        masked = masked.replace(compact(label), " " * len(compact(label)))
    requests = []
    labels = {**excludes, **toppings}
    for key, label in labels.items():
        for mention in re.finditer(re.escape(compact(label)), masked):
            suffix = masked[mention.end():]
            prefix = re.match(r"(?:을|를|은|는)?(?:토핑(?:을|은|의)?)?(?:좀)?", suffix)
            rest = suffix[prefix.end():]
            command = None
            operation = None
            # Relative clauses alone (추가한 거요 / 제외한 거요) are selectors.
            restore = re.match(
                r"(?:(?:뺀|빼놓은|빼는|제외한|제외된)(?:거|것)?(?:를|을)?"
                r"(?P<scope>전부|모두|둘다)?취소(?:해주세요|해줘|해)?"
                r"|제외(?:한거)?취소(?:해주세요|해줘|해)?"
                r"|다시넣(?:어주세요|어줘|기|어)?|복구(?:해주세요|해줘|해)?"
                r"|원래대로(?:해주세요|해줘)?|빼지말(?:아주세요|아줘)?"
                r"|제외하지말(?:아주세요|아줘)?)", rest)
            reverse = re.match(
                r"(?:(?:추가한|추가했던|추가해둔|추가)(?:거|것)?(?:를|을)?"
                r"(?P<scope>전부|모두|둘다)?취소(?:해주세요|해줘|해)?"
                r"|취소(?:해주세요|해줘|해)?|제거(?:해주세요|해줘|해)?"
                r"|빼(?:주세요|줘|기)?)", rest)
            exclude = re.match(r"(?:빼(?:주세요|줘|기)?|제외(?!한|된)(?:해주세요|해줘|해)?|없이(?!한|된)(?:해주세요|해줘|해)?)", rest)
            add = re.match(r"(?:추가(?!한|된|했던|해둔)(?:해주세요|해줘|해)?|넣어(?:주세요|줘)|올려(?:주세요|줘))", rest)
            if key in excludes and "토핑" in prefix.group() and (restore or reverse or exclude or add):
                return {"kind": "unsupported", "label": label}
            if key in excludes and restore:
                command, operation = restore, "exclude_remove"
            elif key in toppings and reverse:
                command, operation = reverse, "topping_remove"
            elif key in excludes and exclude:
                command, operation = exclude, "exclude_add"
            elif key in toppings and add:
                command, operation = add, "topping_add"
            elif key in excludes and (add or ("토핑" in prefix.group() and reverse)):
                return {"kind": "unsupported", "label": label}
            if command:
                end = mention.end() + prefix.end() + command.end()
                scope = command.groupdict().get("scope") or ""
                requests.append({
                    "operation": operation, "key": key, "label": label,
                    "selector_text": value[:mention.start()] + scope + value[end:],
                })
    if len(requests) > 1:
        return {"kind": "multiple_requests"}
    return requests[0] if requests else None


def normalize_target_followup(text, operation, toppings, excludes):
    """Remove a repeated pending command after an existing-option target.

    Only an explicit locative target (e.g. '베이컨 추가한 버거에') qualifies.
    Bare menu add orders and newly specified ingredient operations are left
    to the caller's normal request detection.
    """
    value = compact(text)
    commands = {
        "topping_add": r"(?:추가(?:해주세요|해줘|해)|넣어(?:주세요|줘)|올려(?:주세요|줘))",
        "topping_remove": r"(?:빼(?:주세요|줘)|제거(?:해주세요|해줘|해))",
        "exclude_add": r"(?:빼(?:주세요|줘)|제외(?:해주세요|해줘|해))",
        "exclude_remove": r"(?:다시넣어(?:주세요|줘)|복구(?:해주세요|해줘|해))",
    }
    command = commands.get(operation)
    if command is None:
        return text
    match = re.fullmatch(r"(?P<target>.+(?:에서|에는|에도|에))(?:좀)?" + command + r"요?", value)
    if match is None:
        return text
    target = match['target']
    for mapping, feature in (
        (toppings, r"(?:토핑)?(?:추가한|추가된|넣은|넣었던|올린|얹은|토핑있는|토핑들어간)"),
        (excludes, r"(?:뺀|뺐|빼놓은|제외한|제외된|없는|없이한)"),
    ):
        for label in mapping.values():
            if re.search(re.escape(compact(label)) + r"(?:을|를)?" + feature, target):
                return target
    return text


def explicit_new_order(text, menus, drinks, sides):
    value = compact(text)
    names = [*menus.values(), *drinks.values(), *sides.values()]
    return any(compact(x) in value for x in names) and bool(re.search(
        r"주문|주세요|추가해|취소|삭제|변경|바꿔|빼주세요|더줘", value))


def resolve_targets(text, candidates, mappings, reference_items=None):
    """Union selector groups; intersect predicates only within each group.

    Numeric references are stable line IDs. Ordinals use the displayed order,
    never renumber an eligibility subset. Counts select the first N eligible
    items in the current group, independently of their option signatures.
    """
    value = compact(text)
    items = sorted(candidates, key=lambda item: item["line_id"])
    references = list(reference_items if reference_items is not None else items)
    eligible = {item["line_id"] for item in items}
    if not items:
        return {"status": "not_found", "items": []}

    def result(status, selected):
        ids = {item["line_id"] for item in selected}
        return {"status": status, "items": [item for item in items if item["line_id"] in ids]}

    if re.search(r"말고|아닌|아니고|또는|아무거나말고", value):
        return result("unknown", [])

    count_pattern = r"\d+개|열개|아홉개|여덟개|일곱개|여섯개|다섯개|네개|세개|두개|한개|하나|둘|셋|넷"
    counts = {"열개": 10, "아홉개": 9, "여덟개": 8, "일곱개": 7,
              "여섯개": 6, "다섯개": 5, "네개": 4, "넷": 4,
              "세개": 3, "셋": 3, "두개": 2, "둘": 2, "한개": 1, "하나": 1}
    all_requested = bool(re.search(r"전부|모두|전체", value))
    if value in {"다", "다요"}:
        all_requested = True
    pair_requested = "둘다" in value or "두개다" in value
    global_pair = pair_requested and not re.search(count_pattern, value.replace("둘다", "").replace("두개다", ""))
    all_requested |= global_pair

    def take(group, clause, criteria):
        if not group:
            return result("not_found", [])
        pair = "둘다" in clause or "두개다" in clause
        if pair and not global_pair:
            return result("resolved" if len(group) == 2 else "invalid_count", group)
        found = re.findall(count_pattern, clause.replace("둘다", "").replace("두개다", "") if global_pair else clause)
        if len(found) > 1:
            return result("unknown", [])
        if found:
            token = found[0]
            count = int(token[:-1]) if token[0].isdigit() else counts[token]
            if count < 1 or count > len(group):
                return result("invalid_count", group)
            return result("resolved", group[:count])
        if all_requested:
            return result("resolved", group)
        if criteria:
            return result("resolved" if len(group) == 1 else "ambiguous", group)
        return result("unknown", [])

    selected = []
    spans = []
    # Resolve explicit refs first, but do not drop the other selector groups.
    ordinal_words = ["첫", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉", "열"]
    ordinal_pattern = "|".join(re.escape(x) + "번째" for x in ordinal_words)
    ordinal_pattern += r"|첫째|둘째|셋째|넷째|마지막"
    for match in re.finditer(r"\d+번|" + ordinal_pattern, value):
        token = match.group()
        if token[0].isdigit():
            target = next((i for i in references if i["line_id"] == int(token[:-1])), None)
        else:
            if token == "마지막":
                position = len(references)
            elif token in {"첫째", "둘째", "셋째", "넷째"}:
                position = ["첫째", "둘째", "셋째", "넷째"].index(token) + 1
            else:
                position = ordinal_words.index(token[:-2]) + 1
            target = references[position - 1] if 0 < position <= len(references) else None
        if target is None or target["line_id"] not in eligible:
            return result("not_found", [])
        selected.append(target)
        spans.append(match.span())
    for start, end in reversed(spans):
        value = value[:start] + "|" + value[end:]

    def detect(mapping, clause):
        work, found = clause, []
        for key, label in sorted(mapping.items(), key=lambda x: len(x[1]), reverse=True):
            token = compact(label)
            if token in work:
                found.append(key)
                work = work.replace(token, "")
        return found

    def filter_group(clause):
        pool = items
        used = False
        remaining = clause
        for field in ("menu", "type", "drink", "drink_size", "side"):
            keys = detect(mappings[field], clause)
            if keys:
                used = True
                pool = [i for i in pool if i.get(field) in keys]
                for key in keys:
                    remaining = remaining.replace(compact(mappings[field][key]), "")
        for field, expressions in (
            ("add_toppings", r"(?:토핑)?(?:추가한|추가된|넣은|넣었던|올린|얹은|토핑있는|토핑들어간)"),
            ("exclude", r"(?:뺀|뺐|빼놓은|제외한|제외된|없는|없이한)"),
        ):
            for key, label in mappings[field].items():
                pattern = re.escape(compact(label)) + r"(?:을|를)?" + expressions
                if re.search(pattern, clause):
                    used = True
                    pool = [i for i in pool if key in (i.get(field) or [])]
                    remaining = re.sub(pattern, "", remaining)
        remaining = re.sub(count_pattern, "", remaining)
        remaining = re.sub(r"전부|모두|전체|둘다|두개다", "", remaining)
        understood = re.fullmatch(
            r"(?:들어있는|들어간|빼주세요|빼줘|주세요|에서|에는|에요|이요|으로|"
            r"주문|항목|버거|그중|중|에|만|인|거|것|요|다|을|를|이|가|의|좀)*", remaining)
        return pool, used, bool(understood)

    # A menu starts a group; an immediately preceding attributive feature
    # belongs to that menu ("치즈버거 하나 베이컨 추가한 치킨버거 하나").
    menu_pattern = "|".join(re.escape(compact(x)) for x in sorted(mappings["menu"].values(), key=len, reverse=True))
    feature_labels = [*mappings["add_toppings"].values(), *mappings["exclude"].values(),
                      *mappings["drink"].values(), *mappings["side"].values(), *mappings["drink_size"].values()]
    feature_pattern = "(?:" + "|".join(re.escape(compact(x)) for x in sorted(feature_labels, key=len, reverse=True)) + r")(?:을|를)?(?:토핑)?(?:추가한|넣은|뺀|제외한|없는|들어간|인)"
    clauses = []
    for piece in re.split(r"\||그리고|이랑|하고|랑|및", value):
        mentions = list(re.finditer(menu_pattern, piece))
        starts = [0]
        for index in range(1, len(mentions)):
            previous, current = mentions[index - 1], mentions[index]
            between = piece[previous.end():current.start()]
            features = list(re.finditer(feature_pattern, between))
            boundary = current.start()
            if features and not between.startswith("중"):
                boundary = previous.end() + features[0].start()
            starts.append(boundary)
        starts.append(len(piece))
        clauses.extend(piece[a:b] for a, b in zip(starts, starts[1:]) if piece[a:b])

    ambiguous = False
    meaningful = False
    for clause in clauses:
        pool, used, understood = filter_group(clause)
        has_count = bool(re.search(count_pattern, clause))
        has_scope = bool(re.search(r"전부|모두|전체|둘다|두개다", clause)) or clause in {"다", "다요"}
        if not (used or has_count or has_scope):
            # Politeness/particles remaining after an explicit number are fine.
            if not re.fullmatch(r"(?:요|에|에서|에요|만|번|거|것|이|를|을|주세요|버거|항목|주문)*", clause):
                return result("unknown", [])
            continue
        meaningful = True
        if not understood:
            return result("unknown", [])
        resolution = take(pool, clause, used)
        if resolution["status"] in {"invalid_count", "not_found", "unknown"}:
            return resolution
        ambiguous |= resolution["status"] == "ambiguous"
        selected.extend(resolution["items"])
    if selected:
        # "둘 다" over a union must mean exactly two, not every candidate.
        unique = result("ambiguous" if ambiguous else "resolved", selected)
        if pair_requested and len(unique["items"]) != 2:
            return result("invalid_count", unique["items"])
        return unique
    return result("unknown" if not meaningful else "not_found", [])
