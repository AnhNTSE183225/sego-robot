import json
import threading
import time

try:
    from confluent_kafka import Consumer, Producer, KafkaException
except ImportError:
    Consumer = None
    Producer = None
    KafkaException = Exception


class KafkaBridge:
    """Lightweight Kafka helper: consume commands/map defs; publish telemetry/acks/map verdicts."""

    def __init__(self, robot_id, bootstrap, topics, on_command, on_map_def, logger=print):
        self.robot_id = robot_id
        self.bootstrap = bootstrap
        self.topics = topics
        self.on_command = on_command
        self.on_map_def = on_map_def
        self.log = logger

        self.producer = None
        self.consumer = None
        self.running = False
        self.consumer_thread = None
        self.heartbeat_thread = None

    def start(self):
        if Consumer is None or Producer is None:
            self.log("Kafka client not installed; skipping Kafka bridge.")
            return False
        consumer_conf = {
            "bootstrap.servers": self.bootstrap,
            "group.id": f"robot-{self.robot_id}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": True,
        }
        producer_conf = {
            "bootstrap.servers": self.bootstrap,
            "client.id": f"robot-{self.robot_id}",
            "enable.idempotence": True,
            "acks": "all",
        }
        try:
            self.consumer = Consumer(consumer_conf)
            self.consumer.subscribe([self.topics["command"], self.topics["map"]])
            self.producer = Producer(producer_conf)
        except Exception as exc:
            self.log(f"Failed to start Kafka bridge: {exc}")
            return False

        self.running = True
        self.consumer_thread = threading.Thread(target=self._consume_loop, daemon=True)
        self.consumer_thread.start()
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()
        self.log(f"Kafka bridge connected to {self.bootstrap}")
        return True

    def stop(self):
        self.running = False
        if self.consumer:
            try:
                self.consumer.close()
            except Exception:
                pass
        if self.consumer_thread:
            self.consumer_thread.join(timeout=2)
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=2)
        if self.producer:
            try:
                self.producer.flush(1.0)
            except Exception:
                pass

    def send(self, topic, msg_type, payload=None, correlation_id=None, seq=None):
        if self.producer is None:
            return
        envelope = {
            "robotId": self.robot_id,
            "msgType": msg_type,
            "correlationId": correlation_id or f"{int(time.time()*1000)}",
            "timestamp": int(time.time() * 1000),
            "seq": seq or int(time.time() * 1000),
            "payload": payload or {},
        }
        try:
            self.producer.produce(topic, key=self.robot_id, value=json.dumps(envelope))
            self.producer.poll(0)
        except BufferError:
            self.producer.poll(0.5)
            self.producer.produce(topic, key=self.robot_id, value=json.dumps(envelope))
        except KafkaException as exc:
            self.log(f"Kafka send failed: {exc}")

    def send_status(self, payload):
        self.send(self.topics["telemetry"], "STATUS", payload)

    def send_ack(self, correlation_id, command, ok, note=None):
        payload = {"command": command, "status": "OK" if ok else "FAILED"}
        if note:
            payload["note"] = note
        self.send(self.topics["telemetry"], "ACK", payload, correlation_id=correlation_id)

    def send_map_verdict(self, correlation_id, map_id, status, reason=None, details=None):
        payload = {
            "mapId": map_id,
            "status": status,
            "reason": reason or "",
            "details": details or {},
        }
        self.send(self.topics["map"], "MAP_VERDICT", payload, correlation_id=correlation_id)

    def send_heartbeat(self):
        self.send(self.topics["telemetry"], "HEARTBEAT", {})

    def _consume_loop(self):
        while self.running and self.consumer:
            try:
                msg = self.consumer.poll(0.5)
            except KafkaException as exc:
                self.log(f"Kafka poll error: {exc}")
                continue
            if msg is None:
                continue
            if msg.error():
                self.log(f"Kafka error: {msg.error()}")
                continue
            try:
                data = json.loads(msg.value().decode("utf-8"))
            except Exception as exc:
                self.log(f"Invalid Kafka message: {exc}")
                continue
            if data.get("robotId") != self.robot_id:
                continue
            msg_type = (data.get("msgType") or "").upper()
            if msg_type == "CMD":
                self.on_command(data)
            elif msg_type == "MAP_DEF":
                self.on_map_def(data)

    def _heartbeat_loop(self):
        while self.running:
            self.send_heartbeat()
            time.sleep(10)
