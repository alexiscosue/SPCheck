// SPCheck Combined Arduino Sketch
// Handles RC522 RFID reader + Adafruit fingerprint sensor on one Arduino
//
// Pin connections:
//   RFID RC522  → SS=10, RST=7, MOSI=11, MISO=12, SCK=13
//   Fingerprint → RX=2 (green wire from sensor TX), TX=3 (white wire to sensor RX)

#include <SPI.h>
#include <MFRC522.h>
#include <SoftwareSerial.h>
#include <Adafruit_Fingerprint.h>

// --- RFID ---
#define SS_PIN  10
#define RST_PIN  7
MFRC522 rfid(SS_PIN, RST_PIN);

// --- Fingerprint ---
SoftwareSerial fingerprintSerial(2, 3);  // RX=2, TX=3
Adafruit_Fingerprint finger = Adafruit_Fingerprint(&fingerprintSerial);

// ------------------------------------------------------------------ setup ---
void setup() {
  Serial.begin(9600);
  delay(100);

  // Init RFID
  SPI.begin();
  rfid.PCD_Init();

  // Init fingerprint sensor
  finger.begin(57600);
  delay(5);

  if (finger.verifyPassword()) {
    Serial.println("BIOMETRIC:READY");
  } else {
    Serial.println("BIOMETRIC:ERROR:Sensor not found");
    while (1) { delay(1); }
  }

  finger.getTemplateCount();
  if (finger.templateCount == 0) {
    Serial.println("BIOMETRIC:WARNING:No fingerprints enrolled");
  } else {
    Serial.print("BIOMETRIC:INFO:Templates=");
    Serial.println(finger.templateCount);
  }

  Serial.println("BIOMETRIC:Waiting for finger...");
}

// ------------------------------------------------------------------- loop ---
void loop() {
  // Read any response from Python backend
  if (Serial.available()) {
    String response = Serial.readStringUntil('\n');
    if (response.startsWith("LOGGED:") || response.startsWith("BUFFER:")) {
      Serial.println(response);
    }
  }

  // --- RFID check ---
  if (rfid.PICC_IsNewCardPresent() && rfid.PICC_ReadCardSerial()) {
    Serial.print(F("RFID Tag UID:"));
    printHex(rfid.uid.uidByte, rfid.uid.size);
    Serial.println("");
    rfid.PICC_HaltA();
    rfid.PCD_StopCrypto1();  // required to reset for next read
  }

  // --- Fingerprint check ---
  int fingerprintID = getFingerprintID();

  if (fingerprintID > 0) {
    Serial.print("Biometric ID:");
    Serial.println(fingerprintID);
    Serial.println("BIOMETRIC:Sending to database...");
    delay(500);
    while (finger.getImage() != FINGERPRINT_NOFINGER) { delay(50); }
    Serial.println("BIOMETRIC:Waiting for finger...");

  } else if (fingerprintID == -2) {
    // Unknown fingerprint - send UNKNOWN so Python can handle it
    Serial.println("Biometric ID:UNKNOWN");
    delay(500);
    while (finger.getImage() != FINGERPRINT_NOFINGER) { delay(50); }
    Serial.println("BIOMETRIC:Waiting for finger...");
  }

  delay(50);
}

// ------------------------------------------------------------ printHex ---
void printHex(byte *buffer, byte bufferSize) {
  for (byte i = 0; i < bufferSize; i++) {
    Serial.print(buffer[i] < 0x10 ? " 0" : " ");
    Serial.print(buffer[i], HEX);
  }
}

// ------------------------------------------------------- getFingerprintID ---
int getFingerprintID() {
  uint8_t p = finger.getImage();
  if (p != FINGERPRINT_OK) return -1;

  p = finger.image2Tz();
  if (p != FINGERPRINT_OK) {
    if (p == FINGERPRINT_IMAGEMESS)
      Serial.println("BIOMETRIC:ERROR:Image too messy");
    else if (p == FINGERPRINT_FEATUREFAIL || p == FINGERPRINT_INVALIDIMAGE)
      Serial.println("BIOMETRIC:ERROR:Could not find fingerprint features");
    return -1;
  }

  p = finger.fingerFastSearch();
  if (p == FINGERPRINT_OK)        return finger.fingerID;
  if (p == FINGERPRINT_NOTFOUND)  return -2;
  if (p == FINGERPRINT_PACKETRECIEVEERR)
    Serial.println("BIOMETRIC:ERROR:Communication error");
  return -1;
}
