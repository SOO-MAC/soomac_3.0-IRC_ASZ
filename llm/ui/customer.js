/* =========================================================
   ASZ DRIVE-THRU CUSTOMER UI
========================================================= */




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
   PRICE TABLE
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

const SET_ICED_COFFEE_EXTRA = 500;
const SET_CHEESE_STICK_EXTRA = 500;


/* =========================================================
   DISPLAY LABEL
========================================================= */

const MENU_LABEL = {
    bulgogi_burger: "불고기버거",
    chicken_burger: "치킨버거",
    cheese_burger: "치즈버거",
    shrimp_burger: "새우버거",
};

const DRINK_LABEL = {
    coke: "콜라",
    zero_coke: "제로콜라",
    sprite: "사이다",
    fanta: "환타",
    iced_coffee: "아이스 아메리카노",
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
    pickle: "피클",
    onion: "양파",
    lettuce: "양상추",
    tomato: "토마토",
};


/* =========================================================
   UI STATE
   현재는 테스트용 가짜 데이터.
   이후 실제 Runtime State가 이 값을 갱신하게 됨.
========================================================= */

const uiState = {

    /*
       listening
       processing
       speaking
       standby
    */
    voiceMode: "listening",

    lastHeard:
        "제로콜라 라지로 주세요.",


    conversation: [

        {
            role: "staff",
            text: "안녕하세요. 주문을 말씀해주세요!"
        },

        {
            role: "customer",
            text: "불고기버거 세트 하나 주세요."
        },

        {
            role: "staff",
            text: "음료를 선택해주세요."
        },

        {
            role: "customer",
            text: "제로콜라 라지로 주세요."
        }

    ],


    items: [

        {
            line_id: 1,

            item_type: "burger",

            quantity: 1,

            menu: "bulgogi_burger",

            type: "set",

            drink: "zero_coke",

            drink_size: "large",

            side: "french_fries",

            exclude: [
                "pickle"
            ],

            add_toppings: []
        },


        {
            line_id: 2,

            item_type: "burger",

            quantity: 1,

            menu: "cheese_burger",

            type: "single",

            drink: null,

            drink_size: null,

            side: null,

            exclude: [],

            add_toppings: [
                "bacon"
            ]
        }

    ]
};


/* =========================================================
   BASIC UTILS
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
   PRICE CALCULATION
========================================================= */

function itemUnitPrice(item) {

    let price = 0;


    /* -----------------------------------------------------
       BURGER
    ----------------------------------------------------- */

    if (item.item_type === "burger") {

        price +=
            BURGER_PRICE[item.menu] || 0;


        /* SET */

        if (item.type === "set") {

            price += SET_UPCHARGE;


            /* 세트 아이스 아메리카노 변경 */

            if (
                item.drink ===
                "iced_coffee"
            ) {
                price +=
                    SET_ICED_COFFEE_EXTRA;
            }


            /* 세트 치즈스틱 변경 */

            if (
                item.side ===
                "cheese_stick"
            ) {
                price +=
                    SET_CHEESE_STICK_EXTRA;
            }


            /* 음료 사이즈 */

            price +=
                SIZE_EXTRA[
                    item.drink_size
                ] || 0;
        }


        /* TOPPING */

        for (
            const topping
            of
            item.add_toppings || []
        ) {

            price +=
                TOPPING_PRICE[
                    topping
                ] || 0;
        }
    }


    /* -----------------------------------------------------
       STANDALONE DRINK
    ----------------------------------------------------- */

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


    /* -----------------------------------------------------
       STANDALONE SIDE
    ----------------------------------------------------- */

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


function itemTotalPrice(item) {

    const quantity =
        Number(
            item.quantity || 1
        );

    return (
        itemUnitPrice(item)
        * quantity
    );
}


function calculateTotal(items) {

    return items.reduce(

        (sum, item) =>
            sum
            + itemTotalPrice(item),

        0
    );
}


/* =========================================================
   ITEM DISPLAY NAME
========================================================= */

function itemDisplayName(item) {

    if (
        item.item_type === "burger"
    ) {

        const burgerName =
            MENU_LABEL[item.menu]
            || item.menu
            || "버거";


        if (item.type === "set") {

            return (
                burgerName
                + " 세트"
            );
        }


        return (
            burgerName
            + " 단품"
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
   OPTION CHIPS
========================================================= */

function buildOptionChips(item) {

    const chips = [];


    /* -----------------------------------------------------
       BURGER SET
    ----------------------------------------------------- */

    if (
        item.item_type === "burger"
        &&
        item.type === "set"
    ) {

        /* DRINK */

        if (item.drink) {

            let text =
                "🥤 "
                + (
                    DRINK_LABEL[
                        item.drink
                    ]
                    || item.drink
                );


            if (
                item.drink ===
                "iced_coffee"
            ) {

                text +=
                    " +"
                    + won(
                        SET_ICED_COFFEE_EXTRA
                    );
            }


            chips.push({

                text,

                className:
                    "option"
            });
        }


        /* DRINK SIZE */

        if (item.drink_size) {

            const size =
                String(
                    item.drink_size
                ).toUpperCase();


            const extra =
                SIZE_EXTRA[
                    item.drink_size
                ] || 0;


            chips.push({

                text:
                    extra > 0
                        ? (
                            size
                            + " +"
                            + won(extra)
                        )
                        : size,

                className:
                    "option"
            });
        }


        /* SIDE */

        if (item.side) {

            let text =
                "🍟 "
                + (
                    SIDE_LABEL[
                        item.side
                    ]
                    || item.side
                );


            if (
                item.side ===
                "cheese_stick"
            ) {

                text +=
                    " +"
                    + won(
                        SET_CHEESE_STICK_EXTRA
                    );
            }


            chips.push({

                text,

                className:
                    "option"
            });
        }
    }


    /* -----------------------------------------------------
       STANDALONE DRINK
    ----------------------------------------------------- */

    if (
        item.item_type === "drink"
    ) {

        if (item.drink_size) {

            const size =
                String(
                    item.drink_size
                ).toUpperCase();


            const extra =
                SIZE_EXTRA[
                    item.drink_size
                ] || 0;


            chips.push({

                text:
                    extra > 0
                        ? (
                            size
                            + " +"
                            + won(extra)
                        )
                        : size,

                className:
                    "option"
            });
        }
    }


    /* -----------------------------------------------------
       TOPPING
    ----------------------------------------------------- */

    for (
        const topping
        of
        item.add_toppings || []
    ) {

        const label =
            TOPPING_LABEL[
                topping
            ]
            || topping;


        const extra =
            TOPPING_PRICE[
                topping
            ]
            || 0;


        let text =
            label
            + " 추가";


        if (extra > 0) {

            text +=
                " +"
                + won(extra);
        }


        chips.push({

            text,

            className:
                "option add"
        });
    }


    /* -----------------------------------------------------
       EXCLUDE
    ----------------------------------------------------- */

    for (
        const excluded
        of
        item.exclude || []
    ) {

        const label =
            EXCLUDE_LABEL[
                excluded
            ]
            || excluded;


        chips.push({

            text:
                label
                + " 제외",

            className:
                "option remove"
        });
    }


    return chips;
}


/* =========================================================
   VOICE STATE
========================================================= */

function renderVoiceState() {

    const statusBox =
        document.getElementById(
            "statusBox"
        );

    const statusSub =
        document.getElementById(
            "statusSub"
        );

    const listenStatus =
        document.getElementById(
            "listenStatus"
        );

    const waveform =
        document.getElementById(
            "waveform"
        );


    const mode =
        uiState.voiceMode;


    /* LISTENING */

    if (mode === "listening") {

        statusBox.textContent =
            "음성 주문 대기 중";

        statusSub.textContent =
            "지금 주문을 말씀해 주세요";

        listenStatus.textContent =
            "LISTENING";

        waveform.classList.remove(
            "inactive"
        );

        return;
    }


    /* PROCESSING */

    if (mode === "processing") {

        statusBox.textContent =
            "주문 내용을 확인하고 있어요";

        statusSub.textContent =
            "잠시만 기다려 주세요";

        listenStatus.textContent =
            "PROCESSING";

        waveform.classList.add(
            "inactive"
        );

        return;
    }


    /* STAFF SPEAKING */

    if (mode === "speaking") {

        statusBox.textContent =
            "STAFF가 안내하고 있어요";

        statusSub.textContent =
            "안내가 끝난 후 말씀해 주세요";

        listenStatus.textContent =
            "SPEAKING";

        waveform.classList.add(
            "inactive"
        );

        return;
    }


    /* COMPLETE */

    if (mode === "complete") {

        statusBox.textContent =
            "주문이 완료되었습니다";

        statusSub.textContent =
            "안내에 따라 앞으로 이동해 주세요";

        listenStatus.textContent =
            "COMPLETE";

        waveform.classList.add(
            "inactive"
        );

        return;
    }


    /* STANDBY */

    statusBox.textContent =
        "음성 주문 준비 중";

    statusSub.textContent =
        "잠시만 기다려 주세요";

    listenStatus.textContent =
        "STANDBY";

    waveform.classList.add(
        "inactive"
    );
}


/* =========================================================
   ORDER RENDER
========================================================= */

function renderOrder() {

    const list =
        document.getElementById(
            "orderList"
        );

    const count =
        document.getElementById(
            "orderCount"
        );

    const total =
        document.getElementById(
            "totalPrice"
        );


    list.innerHTML = "";


    /* 총 상품 수 */

    const quantityTotal =
        uiState.items.reduce(

            (sum, item) =>
                sum
                + Number(
                    item.quantity
                    || 1
                ),

            0
        );


    count.textContent =
        `총 ${quantityTotal}개`;


    /* 총 금액 */

    total.textContent =
        won(
            calculateTotal(
                uiState.items
            )
        );


    /* 주문 없음 */

    if (
        uiState.items.length === 0
    ) {

        list.innerHTML = `

            <div class="empty-order">

                <div class="empty-order-icon">
                    🍔
                </div>

                <div class="empty-order-title">
                    아직 주문한 메뉴가 없습니다
                </div>

                <div class="empty-order-sub">
                    주문하신 메뉴가 여기에 표시됩니다.
                </div>

            </div>
        `;

        return;
    }


    /* 주문 카드 */

    for (
        const item
        of
        groupOrderItems(
            uiState.items
        )
    ) {

        const card =
            document.createElement(
                "div"
            );


        card.className =
            "order-item";


        const chips =
            buildOptionChips(
                item
            );


        const chipHtml =
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


        const quantity =
            Number(
                item.quantity
                || 1
            );


        const quantityHtml =
            quantity > 1
                ? (
                    `
                    <span class="quantity">
                        × ${quantity}
                    </span>
                    `
                )
                : "";


        card.innerHTML = `

            <div class="order-main">

                <div>

                    <span class="order-name">
                        ${escapeHtml(
                            itemDisplayName(
                                item
                            )
                        )}
                    </span>

                    ${quantityHtml}

                </div>


                <span class="order-price">

                    ${won(
                        itemTotalPrice(
                            item
                        )
                    )}

                </span>

            </div>


            ${
                chipHtml
                    ? `
                    <div class="options">
                        ${chipHtml}
                    </div>
                    `
                    : ""
            }
        `;


        list.appendChild(
            card
        );
    }
}


/* =========================================================
   CONVERSATION
========================================================= */

function renderConversation() {

    const box =
        document.getElementById(
            "conversation"
        );


    box.innerHTML = "";


    for (
        const message
        of
        uiState.conversation
    ) {

        const wrapper =
            document.createElement(
                "div"
            );


        /*
           CSS는 기존 호환 때문에
           staff 메시지를 soomac 스타일로 사용.
        */

        const cssRole =
            message.role === "customer"
                ? "customer"
                : "soomac";


        wrapper.className =
            `message ${cssRole}`;


        const label =
            message.role === "customer"
                ? "고객"
                : "STAFF";


        wrapper.innerHTML = `

            <div class="message-label">
                ${label}
            </div>

            <div class="bubble">
                ${escapeHtml(
                    message.text
                )}
            </div>
        `;


        box.appendChild(
            wrapper
        );
    }


    /* 최신 대화로 자동 스크롤 */

    box.scrollTop =
        box.scrollHeight;
}


/* =========================================================
   LAST HEARD
========================================================= */

function renderLastHeard() {

    const element =
        document.getElementById(
            "lastHeard"
        );


    if (!uiState.lastHeard) {

        element.textContent =
            "아직 인식된 음성이 없습니다.";

        return;
    }


    element.textContent =
        `“${uiState.lastHeard}”`;
}


/* =========================================================
   FULL RENDER
========================================================= */

function renderCustomer() {

    renderVoiceState();

    renderOrder();

    renderConversation();

    renderLastHeard();
}


/* =========================================================
   EXTERNAL UPDATE FUNCTIONS
========================================================= */

function addCustomerMessage(text) {

    if (!text) {
        return;
    }


    uiState.lastHeard =
        text;


    uiState.conversation.push({

        role:
            "customer",

        text:
            text
    });


    renderCustomer();
}


function addStaffMessage(text) {

    if (!text) {
        return;
    }


    uiState.conversation.push({

        role:
            "staff",

        text:
            text
    });


    renderCustomer();
}


/*
   기존 코드에서 addSoomacMessage를 호출해도
   깨지지 않도록 호환 alias 유지.
*/

function addSoomacMessage(text) {

    addStaffMessage(text);
}


function setVoiceMode(mode) {

    uiState.voiceMode =
        mode;

    renderVoiceState();
}


function setOrderItems(items) {

    uiState.items =
        Array.isArray(items)
            ? items
            : [];

    renderOrder();
}


function setLastHeard(text) {

    uiState.lastHeard =
        text || "";

    renderLastHeard();
}


function setConversation(
    messages
) {

    uiState.conversation =
        Array.isArray(messages)
            ? messages
            : [];

    renderConversation();
}


/* =========================================================
   INITIAL RENDER
========================================================= */

renderCustomer();


/* =========================================================
   GLOBAL API
   나중에 실제 UI 서버에서도 사용 가능
========================================================= */

window.uiState =
    uiState;

window.renderCustomer =
    renderCustomer;

window.addCustomerMessage =
    addCustomerMessage;

window.addStaffMessage =
    addStaffMessage;

/* backward compatibility */
window.addSoomacMessage =
    addSoomacMessage;

window.setVoiceMode =
    setVoiceMode;

window.setOrderItems =
    setOrderItems;

window.setLastHeard =
    setLastHeard;

window.setConversation =
    setConversation;


/* =========================================================
   REAL DRIVE-THRU APP CONNECTION
========================================================= */

let lastBackendRevision = -1;


async function syncFromDriveThruApp() {

    try {

        const response =
            await fetch(
                "/api/state?_="
                + Date.now(),
                {
                    cache: "no-store"
                }
            );


        if (!response.ok) {
            return;
        }


        const backend =
            await response.json();


        if (
            backend.revision ===
            lastBackendRevision
        ) {
            return;
        }


        lastBackendRevision =
            backend.revision;


        uiState.voiceMode =
            backend.voice_mode
            || "standby";


        uiState.lastHeard =
            backend.last_heard
            || "";


        uiState.conversation =
            Array.isArray(
                backend.conversation
            )
                ? backend.conversation
                : [];


        uiState.items =
            Array.isArray(
                backend.items
            )
                ? backend.items
                : [];


        renderCustomer();

    }

    catch (error) {

        /*
           앱이 꺼져 있을 때
           콘솔을 계속 도배하지 않는다.
        */

    }
}


syncFromDriveThruApp();


setInterval(
    syncFromDriveThruApp,
    300
);
