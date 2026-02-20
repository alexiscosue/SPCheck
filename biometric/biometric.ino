/***************************************************
  Biometric Fingerprint Scanner for SPCheck
  Campus Entry/Exit Logging System

  Based on Adafruit Fingerprint sensor library
  Sends fingerprint ID via Serial for backend processing
 ****************************************************/

#include <Adafruit_Fingerprint.h>

#if (defined(__AVR__) || defined(ESP8266)) && !defined(__AVR_ATmega2560__)
// For UNO and others without hardware serial, use software serial
// pin #2 is IN from sensor (GREEN wire)
// pin #3 is OUT from arduino (WHITE wire)
SoftwareSerial mySerial(2, 3);
#else
// On Leonardo/M0/etc, others with hardware serial, use hardware serial!
// #0 is green wire, #1 is white
#define mySerial Serial1
#endif

Adafruit_Fingerprint finger = Adafruit_Fingerprint(&mySerial);

void setup()
{
  Serial.begin(9600);
  while (!Serial);
  delay(100);

  // Initialize fingerprint sensor
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

void loop()
{
  // Check for response from Python backend
  if (Serial.available()) {
    String response = Serial.readStringUntil('\n');
    if (response.startsWith("LOGGED:")) {
      Serial.println(response);  // Echo back: LOGGED:Entry or LOGGED:Exit
    }
  }

  int fingerprintID = getFingerprintID();

  if (fingerprintID > 0) {
    // Valid fingerprint found - send to backend
    Serial.print("Biometric ID: ");
    Serial.println(fingerprintID);
    Serial.println("BIOMETRIC:Sending to database...");

    // Brief wait then check for finger removal
    delay(500);
    while (finger.getImage() != FINGERPRINT_NOFINGER) {
      delay(50);
    }
    Serial.println("BIOMETRIC:Waiting for finger...");
  } else if (fingerprintID == -2) {
    // Unknown fingerprint - wait for finger removal
    Serial.println("BIOMETRIC:Unknown fingerprint!");
    delay(500);
    while (finger.getImage() != FINGERPRINT_NOFINGER) {
      delay(50);
    }
    Serial.println("BIOMETRIC:Waiting for finger...");
  }

  delay(50);
}

int getFingerprintID() {
  uint8_t p = finger.getImage();

  if (p != FINGERPRINT_OK) {
    return -1;
  }

  // Image taken successfully
  p = finger.image2Tz();
  if (p != FINGERPRINT_OK) {
    if (p == FINGERPRINT_IMAGEMESS) {
      Serial.println("BIOMETRIC:ERROR:Image too messy");
    } else if (p == FINGERPRINT_FEATUREFAIL || p == FINGERPRINT_INVALIDIMAGE) {
      Serial.println("BIOMETRIC:ERROR:Could not find fingerprint features");
    }
    return -1;
  }

  // Search for matching fingerprint
  p = finger.fingerFastSearch();

  if (p == FINGERPRINT_OK) {
    // Found a match
    return finger.fingerID;
  } else if (p == FINGERPRINT_NOTFOUND) {
    return -2;
  } else if (p == FINGERPRINT_PACKETRECIEVEERR) {
    Serial.println("BIOMETRIC:ERROR:Communication error");
    return -1;
  } else {
    return -1;
  }
}
