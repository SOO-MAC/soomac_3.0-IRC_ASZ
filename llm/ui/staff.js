/* =========================================================
   ASZ STAFF UI
========================================================= */

const BURGER_PRICE = {
    bulgogi_burger: 4500,
    chicken_burger: 4800,
    cheese_burger: 5000,
    shrimp_burger: 5200,
};

const DRINK_PRICE = {
    coke: 2000,
    zero_coke: 2000,
    sprite: 2000,
    fanta: 2000,
    iced_coffee: 2500,
};

const SIDE_PRICE = {
    french_fries: 2000,
    cheese_stick: 2500,
};

const SIZE_EXTRA = {
    small: 0,
    medium: 300,
    large: 700,
};

const TOPPING_PRICE = {
    cheese: 500,
    bacon: 800,
    tomato: 400,
};

const SET_UPCHARGE = 2500;

const SET_DRINK_EXTRA = {
    coke: 0,
    zero_coke: 0,
    sprite: 0,
    fanta: 0,
    iced_coffee: 500,
};

const SET_SIDE_EXTRA = {
    french_fries: 0,
    cheese_stick: 500,
};

const MENU_LABEL = {
    bulgogi_burger: "불고기버거",
    chicken_burger: "치킨버거",
    cheese_burger: "치즈버거",
    shrimp_burger: "새우버거",
};

const DRINK_LABEL = {
    coke: "콜라",
    zero_coke: "제로콜라",
    sprite: "스프라이트",
    fanta: "환타",
    iced_coffee: "아이스커피",
};

const SIDE_LABEL = {
    french_fries: "감자튀김",
    cheese_stick: "치즈스틱",
};

const TOPPING_LABEL = {
    cheese: "치즈",
    bacon: "베이컨",
    tomato: "토마토",
};

const EXCLUDE_LABEL = {
    onion: "양파",
    pickle: "피클",
    tomato: "토마토",
    cheese: "치즈",
    lettuce: "양상추",
};


let lastRevision = -1;


/* =========================================================
   UTILS
========================================================= */

function won(value) {

    return (
        Number(value || 0)
            .toLocaleString("ko-KR")
        + "원"
    );
}


function escapeHtml(value) {

    return String(value != null ? value : "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}




/* =========================================================
   ASZ UI SAME ITEM GROUPING
   Runtime의 line_id는 유지하고 화면에서만 동일 품목을 ×N으로 묶는다.
========================================================= */

function groupOrderItems(items) {

    const groups = new Map();


    for (const original of items || []) {

        const item = {
            ...original,

            exclude: [
                ...(original.exclude || [])
            ].sort(),

            add_toppings: [
                ...(original.add_toppings || [])
            ].sort(),
        };


        /*
           line_id와 quantity는 묶기 기준에서 제외.

           아래 옵션이 전부 같은 경우에만 같은 상품으로 본다.
        */
        const key = JSON.stringify({

            item_type:
                item.item_type != null ? item.item_type : null,

            menu:
                item.menu != null ? item.menu : null,

            type:
                item.type != null ? item.type : null,

            drink:
                item.drink != null ? item.drink : null,

            drink_size:
                item.drink_size != null ? item.drink_size : null,

            side:
                item.side != null ? item.side : null,

            exclude:
                item.exclude,

            add_toppings:
                item.add_toppings,
        });


        const quantity =
            Number(
                item.quantity || 1
            );


        if (groups.has(key)) {

            groups.get(key).quantity +=
                quantity;

            continue;
        }


        groups.set(
            key,
            {
                ...item,

                /*
                   UI 표시용 대표 line.
                   Runtime의 실제 line들은 변경하지 않는다.
                */
                quantity:
                    quantity,
            }
        );
    }


    return Array.from(
        groups.values()
    );
}


/* =========================================================
   PRICE
========================================================= */

function unitPrice(item) {

    let price = 0;


    if (item.item_type === "burger") {

        price +=
            BURGER_PRICE[item.menu] || 0;


        for (
            const topping
            of item.add_toppings || []
        ) {

            price +=
                TOPPING_PRICE[topping]
                || 0;
        }


        if (item.type === "set") {

            price += SET_UPCHARGE;

            price +=
                SET_DRINK_EXTRA[
                    item.drink
                ] || 0;

            price +=
                SIZE_EXTRA[
                    item.drink_size
                ] || 0;

            price +=
                SET_SIDE_EXTRA[
                    item.side
                ] || 0;
        }
    }


    else if (
        item.item_type === "drink"
    ) {

        price +=
            DRINK_PRICE[
                item.drink
            ] || 0;

        price +=
            SIZE_EXTRA[
                item.drink_size
            ] || 0;
    }


    else if (
        item.item_type === "side"
    ) {

        price +=
            SIDE_PRICE[
                item.side
            ] || 0;
    }


    return price;
}


function totalPrice(items) {

    return items.reduce(

        (total, item) =>
            total
            +
            (
                unitPrice(item)
                *
                Number(
                    item.quantity || 1
                )
            ),

        0
    );
}


/* =========================================================
   DISPLAY NAME
========================================================= */

function itemName(item) {

    if (
        item.item_type === "burger"
    ) {

        const menu =
            MENU_LABEL[item.menu]
            || item.menu
            || "버거";

        return (
            menu
            +
            (
                item.type === "set"
                    ? " 세트"
                    : " 단품"
            )
        );
    }


    if (
        item.item_type === "drink"
    ) {

        return (
            DRINK_LABEL[item.drink]
            || item.drink
            || "음료"
        );
    }


    if (
        item.item_type === "side"
    ) {

        return (
            SIDE_LABEL[item.side]
            || item.side
            || "사이드"
        );
    }


    return "상품";
}


/* =========================================================
   OPTIONS
========================================================= */

function optionChips(item) {

    const result = [];


    if (
        item.item_type === "burger"
        &&
        item.type === "set"
    ) {

        if (item.drink) {

            let text =
                DRINK_LABEL[item.drink]
                || item.drink;


            const extra =
                SET_DRINK_EXTRA[
                    item.drink
                ] || 0;


            if (extra > 0) {

                text +=
                    ` +${won(extra)}`;
            }


            result.push({
                text,
                className:
                    "option",
            });
        }


        if (item.drink_size) {

            const size =
                String(
                    item.drink_size
                ).toUpperCase();


            const extra =
                SIZE_EXTRA[
                    item.drink_size
                ] || 0;


            result.push({

                text:
                    extra > 0
                        ? `${size} +${won(extra)}`
                        : size,

                className:
                    "option",
            });
        }


        if (item.side) {

            let text =
                SIDE_LABEL[item.side]
                || item.side;


            const extra =
                SET_SIDE_EXTRA[
                    item.side
                ] || 0;


            if (extra > 0) {

                text +=
                    ` +${won(extra)}`;
            }


            result.push({
                text,
                className:
                    "option",
            });
        }
    }


    if (
        item.item_type === "drink"
        &&
        item.drink_size
    ) {

        const size =
            String(
                item.drink_size
            ).toUpperCase();

        const extra =
            SIZE_EXTRA[
                item.drink_size
            ] || 0;


        result.push({

            text:
                extra > 0
                    ? `${size} +${won(extra)}`
                    : size,

            className:
                "option",
        });
    }


    for (
        const topping
        of item.add_toppings || []
    ) {

        const label =
            TOPPING_LABEL[topping]
            || topping;


        const extra =
            TOPPING_PRICE[topping]
            || 0;


        result.push({

            text:
                extra > 0
                    ? `${label} 추가 +${won(extra)}`
                    : `${label} 추가`,

            className:
                "option add",
        });
    }


    for (
        const excluded
        of item.exclude || []
    ) {

        const label =
            EXCLUDE_LABEL[excluded]
            || excluded;


        result.push({

            text:
                `${label} 제외`,

            className:
                "option remove",
        });
    }


    return result;
}


/* =========================================================
   RENDER
========================================================= */

function renderStaff(state) {

    const items =
        Array.isArray(state.items)
            ? state.items
            : [];


    const orderNumber =
        document.getElementById(
            "orderNumber"
        );

    const mobileGroup =
        document.getElementById(
            "mobileNumberGroup"
        );

    const mobileNumber =
        document.getElementById(
            "mobileOrderNumber"
        );

    const orderMode =
        document.getElementById(
            "orderMode"
        );

    const staffStatus =
        document.getElementById(
            "staffStatus"
        );

    const orderCount =
        document.getElementById(
            "orderCount"
        );

    const orderList =
        document.getElementById(
            "orderList"
        );

    const totalElement =
        document.getElementById(
            "totalPrice"
        );


    /* =====================================================
       ORDER NUMBER
    ===================================================== */

    if (state.order_id != null) {

        orderNumber.textContent =
            `#${state.order_id}`;
    }

    else {

        orderNumber.textContent =
            "--";
    }


    /* =====================================================
       MOBILE ORDER
    ===================================================== */

    if (
        state.order_mode === "mobile"
        &&
        state.mobile_order_id != null
    ) {

        mobileGroup.style.display =
            "flex";

        mobileNumber.textContent =
            `#${state.mobile_order_id}`;

        orderMode.textContent =
            "맥오더";

        staffStatus.textContent =
            "맥오더 접수 완료";
    }

    else {

        mobileGroup.style.display =
            "none";

        mobileNumber.textContent =
            "--";


        if (state.order_id != null) {

            orderMode.textContent =
                "일반 주문";

            staffStatus.textContent =
                "주문 접수 완료";
        }

        else if (items.length > 0) {

            orderMode.textContent =
                "주문 입력 중";

            staffStatus.textContent =
                "주문 처리 중";
        }

        else {

            orderMode.textContent =
                "주문 접수 전";

            staffStatus.textContent =
                "주문 대기 중";
        }
    }


    /* =====================================================
       STAFF CALL ALERT
    ===================================================== */

    if (state.staff_call_requested) {

        staffStatus.textContent =
            "🚨 직원 호출 요청";

        staffStatus.style.background =
            "#ffe1e1";

        staffStatus.style.color =
            "#a92626";
    }

    else {

        staffStatus.style.background =
            "";

        staffStatus.style.color =
            "";
    }


    /* =====================================================
       ITEM COUNT
    ===================================================== */

    const quantity =
        items.reduce(

            (sum, item) =>
                sum
                +
                Number(
                    item.quantity || 1
                ),

            0
        );


    orderCount.textContent =
        `총 ${quantity}개`;


    /* =====================================================
       TOTAL
    ===================================================== */

    if (
        state.total_price != null
    ) {

        totalElement.textContent =
            won(
                state.total_price
            );
    }

    else {

        totalElement.textContent =
            won(
                totalPrice(items)
            );
    }


    /* =====================================================
       EMPTY
    ===================================================== */

    if (items.length === 0) {

        orderList.innerHTML = `

            <div class="empty">

                <div class="empty-main">
                    주문 대기 중
                </div>

                <div class="empty-sub">
                    주문이 들어오면 여기에 표시됩니다.
                </div>

            </div>
        `;

        return;
    }


    /* =====================================================
       ITEMS
    ===================================================== */

    orderList.innerHTML = "";


    groupOrderItems(
        items
    ).forEach(
        (
            item,
            index
        ) => {

            const quantity =
                Number(
                    item.quantity || 1
                );


            const price =
                unitPrice(item)
                *
                quantity;


            const chips =
                optionChips(item);


            const chipsHtml =
                chips
                .map(
                    chip =>
                        `
                        <span
                            class="${chip.className}"
                        >
                            ${escapeHtml(
                                chip.text
                            )}
                        </span>
                        `
                )
                .join("");


            const card =
                document.createElement(
                    "div"
                );


            card.className =
                "order-item";


            card.innerHTML = `

                <div class="order-main">

                    <div class="order-name-area">

                        <span class="line-number">
                            ${index + 1}
                        </span>

                        <span class="order-name">
                            ${escapeHtml(
                                itemName(item)
                            )}
                        </span>

                        ${
                            quantity > 1
                                ? `
                                <span class="quantity">
                                    × ${quantity}
                                </span>
                                `
                                : ""
                        }

                    </div>


                    <span class="order-price">
                        ${won(price)}
                    </span>

                </div>


                ${
                    chipsHtml
                        ? `
                        <div class="options">
                            ${chipsHtml}
                        </div>
                        `
                        : ""
                }
            `;


            orderList.appendChild(
                card
            );
        }
    );
}


/* =========================================================
   POLLING
========================================================= */

async function syncState() {

    try {

        const response =
            await fetch(
                "/api/state?_="
                + Date.now(),
                {
                    cache:
                        "no-store"
                }
            );


        if (!response.ok) {
            return;
        }


        const state =
            await response.json();


        if (
            state.revision
            ===
            lastRevision
        ) {
            return;
        }


        lastRevision =
            state.revision;


        renderStaff(
            state
        );

    }

    catch (error) {

        /* drive_thru_app 종료 시 조용히 대기 */

    }
}


syncState();


setInterval(
    syncState,
    300
);
