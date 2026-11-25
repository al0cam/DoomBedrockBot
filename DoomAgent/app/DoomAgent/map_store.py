import base64
import datetime
import json
import os
import zlib

ACTOR = "doom-agent"
LIMIT = 90000


def memory_id():
    return os.environ.get("DOOM_MEMORY_ID") or os.environ.get("BEDROCK_AGENTCORE_MEMORY_ID")


def make_client():
    import boto3
    return boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))


def session(level):
    return "map-" + level


def local_path(level):
    folder = os.environ.get("DOOM_MAP_DIR") or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(folder, f"doom_map_{level}.json")


def pack(data):
    blob = base64.b64encode(zlib.compress(json.dumps(data, separators=(",", ":")).encode(), 9)).decode()
    assert len(blob) < LIMIT, f"map blob is {len(blob)} bytes, limit {LIMIT}"
    return blob


def unpack(blob):
    return json.loads(zlib.decompress(base64.b64decode(blob)))


def save(level, data, client=None):
    try:
        mem = memory_id()
        if mem or client:
            blob = pack(data)
            (client or make_client()).create_event(
                memoryId=mem,
                actorId=ACTOR,
                sessionId=session(level),
                eventTimestamp=datetime.datetime.now(datetime.timezone.utc),
                payload=[{"blob": blob}],
            )
        else:
            tmp = local_path(level) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp, local_path(level))
        return True
    except Exception as e:
        print(f"map_store: save failed, continuing without it: {e!r}")
        return False


def load(level, client=None):
    try:
        mem = memory_id()
        if not (mem or client):
            if not os.path.exists(local_path(level)):
                return None
            with open(local_path(level)) as f:
                return json.load(f)
        client = client or make_client()
        newest = None
        token = None
        while True:
            args = {"memoryId": mem, "actorId": ACTOR, "sessionId": session(level),
                    "includePayloads": True, "maxResults": 100}
            if token:
                args["nextToken"] = token
            page = client.list_events(**args)
            for event in page.get("events", []):
                blobs = [p["blob"] for p in event.get("payload", []) if "blob" in p]
                if blobs and (newest is None or event["eventTimestamp"] > newest[0]):
                    newest = (event["eventTimestamp"], blobs[0])
            token = page.get("nextToken")
            if not token:
                break
        return None if newest is None else unpack(newest[1])
    except Exception as e:
        print(f"map_store: load failed, starting with an empty map: {e!r}")
        return None


def demo():
    import tempfile
    import boto3
    from botocore.stub import Stubber

    data = {"version": 1, "tile": 32, "runs": 3, "exit_seen": True,
            "x0": -24, "y0": -152, "rows": ["??..##D.W" * 16] * 90}
    blob = pack(data)
    assert unpack(blob) == data
    print("blob bytes:", len(blob), "for", sum(len(r) for r in data["rows"]), "tiles")

    mem = "doomtest-abcdefghij"
    os.environ["DOOM_MEMORY_ID"] = mem
    client = boto3.client("bedrock-agentcore", region_name="us-west-2",
                          aws_access_key_id="x", aws_secret_access_key="y")
    old = pack({"version": 1, "runs": 1})
    t1 = datetime.datetime(2026, 11, 25, 10, 0, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 11, 25, 11, 0, tzinfo=datetime.timezone.utc)

    def event(n, when, payload):
        return {"memoryId": mem, "actorId": ACTOR, "sessionId": "map-E1M1", "eventId": f"0000001-{n}",
                "eventTimestamp": when, "branch": {"name": "main"}, "payload": payload}

    seen = {}
    real = client.create_event

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    client.create_event = spy
    with Stubber(client) as stub:
        stub.add_response("create_event", {"event": event(1, t2, [{"blob": blob}])})
        assert save("E1M1", data, client)
        stub.add_response(
            "list_events",
            {"events": [event(2, t2, [{"blob": blob}]), event(1, t1, [{"blob": old}])], "nextToken": "more"},
            {"memoryId": mem, "actorId": ACTOR, "sessionId": "map-E1M1", "includePayloads": True, "maxResults": 100},
        )
        stub.add_response(
            "list_events",
            {"events": [event(0, t1, [{"conversational": {"content": {"text": "hi"}, "role": "USER"}}])]},
            {"memoryId": mem, "actorId": ACTOR, "sessionId": "map-E1M1", "includePayloads": True,
             "maxResults": 100, "nextToken": "more"},
        )
        assert load("E1M1", client) == data
        stub.assert_no_pending_responses()
        stub.add_client_error("list_events", "AccessDeniedException")
        assert load("E1M1", client) is None
        stub.add_client_error("create_event", "ThrottledException")
        assert save("E1M1", data, client) is False
    assert seen["payload"] == [{"blob": blob}] and seen["sessionId"] == "map-E1M1" and seen["actorId"] == ACTOR
    print("boto3 request shapes accepted by the SDK model: create_event, list_events (2 pages), newest event wins")

    del os.environ["DOOM_MEMORY_ID"]
    os.environ.pop("BEDROCK_AGENTCORE_MEMORY_ID", None)
    with tempfile.TemporaryDirectory() as folder:
        os.environ["DOOM_MAP_DIR"] = folder
        assert load("E1M1") is None
        assert save("E1M1", data)
        assert load("E1M1") == data
        del os.environ["DOOM_MAP_DIR"]
    print("local file store round trip ok")


if __name__ == "__main__":
    demo()
