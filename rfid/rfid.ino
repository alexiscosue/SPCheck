
//Arduino Code - RC522 Read RFID Tag UID
 
#include <SPI.h>
#include <MFRC522.h>
 
#define SS_PIN 10
#define RST_PIN 7
 
MFRC522 rfid(SS_PIN, RST_PIN);
 
MFRC522::MIFARE_Key key; 
 
void setup() { 
  Serial.begin(9600);
  SPI.begin(); 
  rfid.PCD_Init(); 
}
 
void loop() {
  if ( ! rfid.PICC_IsNewCardPresent())
    return;
 
  if ( ! rfid.PICC_ReadCardSerial())
    return;
 
  MFRC522::PICC_Type piccType = rfid.PICC_GetType(rfid.uid.sak);
 
  Serial.print(F("RFID Tag UID:"));
  printHex(rfid.uid.uidByte, rfid.uid.size);
  Serial.println("");
 
  rfid.PICC_HaltA(); 
}
 
void printHex(byte *buffer, byte bufferSize) {
  for (byte i = 0; i < bufferSize; i++) {
    Serial.print(buffer[i] < 0x10 ? " 0" : " ");
    Serial.print(buffer[i], HEX);
  }
}