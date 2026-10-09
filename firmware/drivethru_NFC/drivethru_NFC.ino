#include <SPI.h>
#include <MFRC522.h>

// ============================================================
// ESP32-S3 PIN SETTING
// ============================================================

#define RFID_SS_PIN      10
#define RFID_SCK_PIN     12
#define RFID_MOSI_PIN    11
#define RFID_MISO_PIN    13
#define RFID_RST_PIN      9

#define BUZZER_PIN        8

// ============================================================
// SETTING
// ============================================================

#define SERIAL_BAUD       115200

// 카드 찍은 뒤 로봇 동작 신호까지 5초
#define PAYMENT_DELAY_MS  5000

// 피에조
#define BEEP_FREQ         2500
#define BEEP_TIME_MS      180


// ============================================================
// RFID
// ============================================================

MFRC522 rfid(
  RFID_SS_PIN,
  RFID_RST_PIN
);


// ============================================================
// BUZZER
// ============================================================

void beep()
{
  tone(
    BUZZER_PIN,
    BEEP_FREQ
  );

  delay(BEEP_TIME_MS);

  noTone(BUZZER_PIN);
}


// ============================================================
// UID PRINT
// ============================================================

void printUID()
{
  for (
    byte i = 0;
    i < rfid.uid.size;
    i++
  )
  {
    if (
      rfid.uid.uidByte[i] < 0x10
    )
    {
      Serial.print("0");
    }

    Serial.print(
      rfid.uid.uidByte[i],
      HEX
    );
  }
}


// ============================================================
// SETUP
// ============================================================

void setup()
{
  Serial.begin(
    SERIAL_BAUD
  );

  delay(1000);


  Serial.println();
  Serial.println(
    "================================"
  );
  Serial.println(
    " IRC RFID PAYMENT TEST"
  );
  Serial.println(
    " ESP32-S3 + RFID-RC522"
  );
  Serial.println(
    "================================"
  );


  // ----------------------------------------------------------
  // Buzzer
  // ----------------------------------------------------------

  pinMode(
    BUZZER_PIN,
    OUTPUT
  );


  // 부저 부팅 확인
  Serial.println(
    "[BUZZER] Test beep"
  );

  beep();


  // ----------------------------------------------------------
  // SPI
  // ----------------------------------------------------------

  SPI.begin(
    RFID_SCK_PIN,
    RFID_MISO_PIN,
    RFID_MOSI_PIN,
    RFID_SS_PIN
  );


  // ----------------------------------------------------------
  // RC522
  // ----------------------------------------------------------

  rfid.PCD_Init();

  delay(100);


  // RC522 버전 확인
  byte version =
    rfid.PCD_ReadRegister(
      MFRC522::VersionReg
    );


  Serial.print(
    "[RC522] Version register = 0x"
  );

  Serial.println(
    version,
    HEX
  );


  // 보통 0x91 또는 0x92
  if (
    version == 0x00 ||
    version == 0xFF
  )
  {
    Serial.println();
    Serial.println(
      "[ERROR] RC522 communication failed"
    );

    Serial.println(
      "Check SPI wiring / 3.3V"
    );

    // 오류음
    for (
      int i = 0;
      i < 3;
      i++
    )
    {
      tone(
        BUZZER_PIN,
        1000
      );

      delay(100);

      noTone(
        BUZZER_PIN
      );

      delay(150);
    }

    while (true)
    {
      delay(1000);
    }
  }


  Serial.println(
    "[OK] RC522 READY"
  );

  Serial.println();

  Serial.println(
    "--------------------------------"
  );

  Serial.println(
    "카드를 RC522에 찍어주세요."
  );

  Serial.println(
    "--------------------------------"
  );

  Serial.println();
}


// ============================================================
// LOOP
// ============================================================

void loop()
{
  // ----------------------------------------------------------
  // 새로운 카드 없음
  // ----------------------------------------------------------

  if (
    !rfid.PICC_IsNewCardPresent()
  )
  {
    delay(20);

    return;
  }


  // ----------------------------------------------------------
  // 카드 UID 읽기 실패
  // ----------------------------------------------------------

  if (
    !rfid.PICC_ReadCardSerial()
  )
  {
    delay(20);

    return;
  }


  // ==========================================================
  // CARD DETECTED
  // ==========================================================

  Serial.println();

  Serial.println(
    "================================"
  );

  Serial.println(
    "[RFID] CARD DETECTED"
  );


  Serial.print(
    "[RFID] UID = "
  );

  printUID();

  Serial.println();


  // ==========================================================
  // 1. 카드 찍자마자 즉시 삑
  // ==========================================================

  Serial.println(
    "[BUZZER] BEEP!"
  );

  beep();


  // ==========================================================
  // 2. 결제 인식
  // ==========================================================

  Serial.println(
    "[PAYMENT] Card detected"
  );

  Serial.println(
    "[PAYMENT] Waiting 5 seconds..."
  );


  // ==========================================================
  // 3. 요청한 5초 delay
  // ==========================================================

  delay(
    PAYMENT_DELAY_MS
  );


  // ==========================================================
  // 4. 5초 후 ROS용 신호 출력
  // ==========================================================

  Serial.print(
    "NFC_PAYMENT:"
  );

  printUID();

  Serial.println();


  Serial.println(
    "[PAYMENT] PAYMENT_DONE SIGNAL SENT"
  );

  Serial.println(
    "[PAYMENT] Robot may start now."
  );

  Serial.println(
    "================================"
  );

  Serial.println();


  // ==========================================================
  // 카드 처리 종료
  // 같은 카드를 계속 올려둔 상태에서 반복 인식 방지
  // ==========================================================

  rfid.PICC_HaltA();

  rfid.PCD_StopCrypto1();


  delay(300);
}