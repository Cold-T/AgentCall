"""Readable transcript exports, including paginated and streamed provider text."""


def transcript_text(store, task_id):
    task = store.task(task_id)
    lines = []
    previous = None
    after = 0
    while True:
        events = store.task_events(task_id, after, 1000, "model.transcript")
        for event in events:
            data = event["data"]
            if not data.get("text"):
                continue
            key = data.get("item_id") or data.get("response_id")
            if (
                data.get("delta")
                and previous
                and previous["role"] == data.get("role")
                and previous["key"] == key
                and not previous["finished"]
            ):
                lines[-1] += data["text"]
                previous["finished"] = data.get("finished")
            else:
                speaker = "AI 助理" if data.get("role") == "assistant" else "对方"
                note = "（被打断，可能包含未播放内容）" if data.get("interrupted") else ""
                lines.append(f"[{event['time']}] {speaker}{note}：{data['text']}")
                previous = {"role": data.get("role"), "key": key, "finished": data.get("finished")}
        if len(events) < 1000:
            break
        after = events[-1]["id"]
    if not lines:
        return None
    return (
        f"AgentCall Transcript\n任务：{task_id}\n目标：{task['input']['goal']}\n\n"
        + "\n\n".join(lines)
        + "\n"
    )
