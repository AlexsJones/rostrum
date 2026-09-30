// TX-26 connection test for the audio chip's control lines, once a second, for pins 18 (SDA) and 19 (SCL):
//   shield 1        the pin reaches the shield and its pull-up has 3.3 V (read with a weak pull-down)
//   shield 0        open joint, or shorted to ground; see the next reading
//   short 0         the pin is dragged to ground (a solder bridge); 1 = no short
//   I2C found       every address that answers (the SGTL5000 is 0x0A)
#include <Wire.h>

int readWith(int pin, int mode) {
  pinMode(pin, mode);                 // weak (~100k) pulls: the shield's 2.2k pull-up, or a short, wins
  delay(2);
  return digitalRead(pin);
}

void setup() { Serial.begin(115200); }

void loop() {
  for (int pin : {18, 19})
    Serial.printf("pin %d: shield %d short %d   ", pin, readWith(pin, INPUT_PULLDOWN), readWith(pin, INPUT_PULLUP));
  Wire.begin();
  Serial.print(" I2C found:");
  int n = 0;
  for (int a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { Serial.printf(" 0x%02X", a); n++; }
  }
  Serial.println(n ? "" : " none");
  Wire.end();
  delay(1000);
}
