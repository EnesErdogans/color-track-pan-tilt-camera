#include <EEPROM.h>
#include <Servo.h>

const byte PAN_PIN = 9;
const byte TILT_PIN = 10;
const byte LASER_PIN = 11;

const int PAN_MIN = 10;
const int PAN_MAX = 170;
const int TILT_MIN = 20;
const int TILT_MAX = 160;
const int DEFAULT_PAN_CENTER = 90;
const int DEFAULT_TILT_CENTER = 90;
const byte CALIBRATION_MAGIC = 0xA7;

// Bu mekanizmada pan servosu fiziksel olarak ters monte.
const int PAN_DIRECTION = -1;
const int TILT_DIRECTION = 1;

Servo panServo;
Servo tiltServo;

int panCenter = DEFAULT_PAN_CENTER;
int tiltCenter = DEFAULT_TILT_CENTER;

float panPosition = DEFAULT_PAN_CENTER;
float tiltPosition = DEFAULT_TILT_CENTER;
float panTarget = DEFAULT_PAN_CENTER;
float tiltTarget = DEFAULT_TILT_CENTER;

unsigned long lastUpdate = 0;
const unsigned long UPDATE_INTERVAL_MS = 20;
const float SERVO_STEP = 0.65;

int lastPanWrite = DEFAULT_PAN_CENTER;
int lastTiltWrite = DEFAULT_TILT_CENTER;

char commandBuffer[48];
byte commandLength = 0;

struct CalibrationData {
  byte magic;
  byte panCenter;
  byte tiltCenter;
  byte checksum;
};

byte calibrationChecksum(byte pan, byte tilt) {
  return CALIBRATION_MAGIC ^ pan ^ tilt;
}

void loadCalibration() {
  CalibrationData data;
  EEPROM.get(0, data);

  bool valid = (
    data.magic == CALIBRATION_MAGIC &&
    data.checksum == calibrationChecksum(data.panCenter, data.tiltCenter) &&
    data.panCenter >= PAN_MIN &&
    data.panCenter <= PAN_MAX &&
    data.tiltCenter >= TILT_MIN &&
    data.tiltCenter <= TILT_MAX
  );

  if (valid) {
    panCenter = data.panCenter;
    tiltCenter = data.tiltCenter;
  }
}

void saveCalibration() {
  CalibrationData data = {
    CALIBRATION_MAGIC,
    (byte)panCenter,
    (byte)tiltCenter,
    calibrationChecksum((byte)panCenter, (byte)tiltCenter)
  };
  EEPROM.put(0, data);
}

void printCalibration() {
  Serial.print("CAL,");
  Serial.print(panCenter);
  Serial.print(",");
  Serial.println(tiltCenter);
}

float moveToward(float current, float target, float stepSize) {
  if (current < target) return min(current + stepSize, target);
  if (current > target) return max(current - stepSize, target);
  return current;
}

void centerServos() {
  panTarget = panCenter;
  tiltTarget = tiltCenter;
  digitalWrite(LASER_PIN, LOW);
}

void handleCommand(char *command) {
  if (command[0] == 'C' || command[0] == 'c') {
    centerServos();
    Serial.println("OK,CENTER");
    return;
  }

  if (command[0] == 'Q' || command[0] == 'q') {
    printCalibration();
    return;
  }

  int absolutePan;
  int absoluteTilt;
  if (sscanf(command, "P,%d,%d", &absolutePan, &absoluteTilt) == 2 ||
      sscanf(command, "p,%d,%d", &absolutePan, &absoluteTilt) == 2) {
    panTarget = constrain(absolutePan, PAN_MIN, PAN_MAX);
    tiltTarget = constrain(absoluteTilt, TILT_MIN, TILT_MAX);
    Serial.println("OK,PREVIEW");
    return;
  }

  int savedPan;
  int savedTilt;
  if (sscanf(command, "K,%d,%d", &savedPan, &savedTilt) == 2 ||
      sscanf(command, "k,%d,%d", &savedPan, &savedTilt) == 2) {
    panCenter = constrain(savedPan, PAN_MIN, PAN_MAX);
    tiltCenter = constrain(savedTilt, TILT_MIN, TILT_MAX);
    saveCalibration();
    centerServos();
    Serial.println("OK,CALIBRATION");
    printCalibration();
    return;
  }

  int panDelta;
  int tiltDelta;
  if (sscanf(command, "M,%d,%d", &panDelta, &tiltDelta) == 2 ||
      sscanf(command, "m,%d,%d", &panDelta, &tiltDelta) == 2) {
    panTarget = constrain(
      panTarget + panDelta * PAN_DIRECTION,
      PAN_MIN,
      PAN_MAX
    );
    tiltTarget = constrain(
      tiltTarget + tiltDelta * TILT_DIRECTION,
      TILT_MIN,
      TILT_MAX
    );
    Serial.println("OK,MOVE");
    return;
  }

  int laserEnabled;
  if (sscanf(command, "L,%d", &laserEnabled) == 1 ||
      sscanf(command, "l,%d", &laserEnabled) == 1) {
    digitalWrite(LASER_PIN, laserEnabled ? HIGH : LOW);
    Serial.println("OK,LASER");
    return;
  }

  Serial.println("ERR,COMMAND");
}

void readSerial() {
  while (Serial.available() > 0) {
    char value = Serial.read();
    if (value == '\n' || value == '\r') {
      if (commandLength > 0) {
        commandBuffer[commandLength] = '\0';
        handleCommand(commandBuffer);
        commandLength = 0;
      }
    } else if (commandLength < sizeof(commandBuffer) - 1) {
      commandBuffer[commandLength++] = value;
    } else {
      commandLength = 0;
    }
  }
}

void updateServos() {
  unsigned long now = millis();
  if (now - lastUpdate < UPDATE_INTERVAL_MS) return;
  lastUpdate = now;

  panPosition = moveToward(panPosition, panTarget, SERVO_STEP);
  tiltPosition = moveToward(tiltPosition, tiltTarget, SERVO_STEP);

  int panAngle = (int)round(panPosition);
  int tiltAngle = (int)round(tiltPosition);

  if (panAngle != lastPanWrite) {
    panServo.write(panAngle);
    lastPanWrite = panAngle;
  }

  if (tiltAngle != lastTiltWrite) {
    tiltServo.write(tiltAngle);
    lastTiltWrite = tiltAngle;
  }
}

void setup() {
  Serial.begin(115200);
  loadCalibration();

  panPosition = panCenter;
  tiltPosition = tiltCenter;
  panTarget = panCenter;
  tiltTarget = tiltCenter;
  lastPanWrite = panCenter;
  lastTiltWrite = tiltCenter;

  pinMode(LASER_PIN, OUTPUT);
  digitalWrite(LASER_PIN, LOW);

  panServo.attach(PAN_PIN);
  tiltServo.attach(TILT_PIN);
  panServo.write(panCenter);
  tiltServo.write(tiltCenter);

  Serial.println("READY,PAN_TILT");
  printCalibration();
}

void loop() {
  readSerial();
  updateServos();
}
