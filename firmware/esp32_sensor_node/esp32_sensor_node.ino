#include <Arduino.h>

// Hardware Pin Specifications
const int LDR_PIN = 39;       // Pre-wired on-board LDR analog line
const int PIR_PIN = 25;       // External PIR sensor digital input
const int TRIG_PIN = 26;      // External Ultrasonic Trigger output pin
const int ECHO_PIN = 27;      // External Ultrasonic Echo input pin (via voltage divider)

// Asynchronous Hardware Timing Variables
volatile unsigned long pulseStartMicros = 0;
volatile unsigned long pulseDurationMicros = 0;
volatile bool bounceCaptured = false;

unsigned long lastTriggerMillis = 0;
const unsigned long triggerIntervalMillis = 60; // Sample at ~16.6 Hz loop pace

// Strict fixed-width packed data structure (7 Bytes total)
struct __attribute__((__packed__)) SensorPacket {
  uint8_t pir_state;       // 1 Byte
  uint16_t ldr_value;      // 2 Bytes
  float distance_cm;       // 4 Bytes
};

// Interrupt Service Routine (ISR) for Echo Line Transitions
void IRAM_ATTR echoInterruptHandler() {
  unsigned long currentMicros = micros();
  if (digitalRead(ECHO_PIN) == HIGH) {
    pulseStartMicros = currentMicros;
  } else {
    if (pulseStartMicros != 0) {
      pulseDurationMicros = currentMicros - pulseStartMicros;
      bounceCaptured = true;
    }
  }
}

void setup() {
  // Initialize the binary hardware pipe channel
  Serial.begin(115200);
  
  pinMode(LDR_PIN, INPUT);
  pinMode(PIR_PIN, INPUT);
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);
  
  digitalWrite(TRIG_PIN, LOW);
  
  // Bind hardware pin change handlers to capture edge timing changes instantly
  attachInterrupt(digitalPinToInterrupt(ECHO_PIN), echoInterruptHandler, CHANGE);
}

void loop() {
  unsigned long currentMillis = millis();
  static float lastValidDistance = 300.0;

  // 1. Asynchronously cycle the Ultrasonic sensor without holding execution threads
  if (currentMillis - lastTriggerMillis >= triggerIntervalMillis) {
    lastTriggerMillis = currentMillis;
    
    // Generate the 10-microsecond excitation heartbeat pulse
    digitalWrite(TRIG_PIN, HIGH);
    delayMicroseconds(10);
    digitalWrite(TRIG_PIN, LOW);
  }

  // 2. Poll for fresh completed sonar calculations
  if (bounceCaptured) {
    noInterrupts(); // Temporarily preserve volatile register updates
    unsigned long dynamicDuration = pulseDurationMicros;
    bounceCaptured = false;
    interrupts();
    
    lastValidDistance = (dynamicDuration * 0.0343) / 2.0;
    // Cap limits to remove system tracking outliers
    if (lastValidDistance > 500.0 || lastValidDistance <= 0) {
      lastValidDistance = -1.0; 
    }
  }

  // 3. Assemble the optimized data packet framework directly in memory
  SensorPacket packet;
  packet.pir_state = static_cast<uint8_t>(digitalRead(PIR_PIN));
  
  int rawLDR = analogRead(LDR_PIN);
  packet.ldr_value = static_cast<uint16_t>(map(rawLDR, 0, 4095, 0, 1023));
  packet.distance_cm = lastValidDistance;

  // 4. Blast the raw structured bytes directly down the USB channel
  Serial.write(reinterpret_cast<uint8_t*>(&packet), sizeof(packet));
  
  // Tiny microsecond pause yields thread slices smoothly to background radio tasks
  delayMicroseconds(500);
}