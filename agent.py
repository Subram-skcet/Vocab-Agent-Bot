import os
import sys
import datetime
import asyncio
import shutil
import subprocess
import time
import json
import email.utils
from pathlib import Path

import requests
from dotenv import load_dotenv
import edge_tts

# This file is UTF-8 and full of emoji. The Windows console is cp1252, so soften
# stdout rather than letting an unprintable character abort a finished run.
sys.stdout.reconfigure(errors="replace")

# Load environment variables
load_dotenv()
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

# The script is written by Claude Code running headless, which bills against the Claude
# subscription instead of an API key. Claude Code resolves its own credentials, so a
# missing token here is a warning rather than a failure: an interactive login on a
# developer machine is equally valid. In CI there is no other credential, so an absent
# token surfaces as a failed generation a few seconds later, which is loud enough.
if not os.getenv("CLAUDE_CODE_OAUTH_TOKEN"):
    print(
        "[auth] CLAUDE_CODE_OAUTH_TOKEN is not set. Falling back to whatever credential "
        "Claude Code finds. Generate a token with 'claude setup-token' if this is CI."
    )

# Spaced Repetition Schedule (Review Stage -> Days to add for next review)
INTERVALS = {
    0: 1,   # Stage 0 (New) -> Review in 1 day
    1: 3,   # Stage 1 -> Review in 3 days
    2: 7,   # Stage 2 -> Review in 7 days
    3: 14,  # Stage 3 -> Review in 14 days
    4: 30,  # Stage 4 -> Review in 30 days
    5: 90,  # Stage 5 -> Review in 90 days
}

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

MAX_RETRIES = 5
RETRY_DELAY_SECONDS = 3
REQUEST_TIMEOUT_SECONDS = 30
NEW_WORD_DAILY_LIMIT = 5

def run_with_retries(operation_name, action):
    """Runs a sync operation with retries for transient external-service failures."""
    total_attempts = MAX_RETRIES + 1
    
    for attempt in range(1, total_attempts + 1):
        try:
            return action()
        except Exception as error:
            if attempt == total_attempts:
                print(f"[retry] {operation_name} failed after {total_attempts} attempts: {error}")
                raise
                
            print(
                f"[retry] {operation_name} failed on attempt {attempt}/{total_attempts}: "
                f"{error}. Retrying in {RETRY_DELAY_SECONDS} seconds..."
            )
            time.sleep(RETRY_DELAY_SECONDS)

async def run_async_with_retries(operation_name, action):
    """Runs an async operation with retries for transient external-service failures."""
    total_attempts = MAX_RETRIES + 1
    
    for attempt in range(1, total_attempts + 1):
        try:
            return await action()
        except Exception as error:
            if attempt == total_attempts:
                print(f"[retry] {operation_name} failed after {total_attempts} attempts: {error}")
                raise
                
            print(
                f"[retry] {operation_name} failed on attempt {attempt}/{total_attempts}: "
                f"{error}. Retrying in {RETRY_DELAY_SECONDS} seconds..."
            )
            await asyncio.sleep(RETRY_DELAY_SECONDS)

def send_notion_request(method, url, operation_name, **kwargs):
    """Sends a Notion API request with timeout, status validation, and retries."""
    def request_once():
        response = requests.request(
            method,
            url,
            headers=NOTION_HEADERS,
            timeout=REQUEST_TIMEOUT_SECONDS,
            **kwargs
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            raise requests.HTTPError(f"{error}. Response: {response.text}") from error
        return response

    return run_with_retries(operation_name, request_once)

def get_todays_vocab():
    """Queries Notion for terms where Next Review Date <= Today."""
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    today = datetime.date.today().isoformat()
    
    payload = {
        "filter": {
            "property": "Next Review Date",
            "date": {
                "on_or_before": today
            }
        },
        "sorts": [
            {
                "property": "Next Review Date",
                "direction": "ascending"
            },
            {
                "timestamp": "created_time",
                "direction": "ascending"
            }
        ]
    }
    
    response = send_notion_request("post", url, "Querying Notion database", json=payload)
    if response.status_code != 200:
        print("❌ Error querying Notion:", response.text)
        return []
        
    results = response.json().get("results", [])
    vocab_list = []
    
    for page in results:
        page_id = page["id"]
        props = page["properties"]
        
        # Safely extract text from Title property
        title_list = props["Term"]["title"]
        term = title_list[0]["text"]["content"] if title_list else ""
        
        # Safely extract Select property
        term_type = props["Type"]["select"]["name"] if props["Type"]["select"] else "Word"
        
        # Safely extract Stage property
        stage = props["Review Stage"]["number"] if props["Review Stage"]["number"] is not None else 0
        count_property = props.get("Count", {})
        count = count_property.get("number") if count_property.get("number") is not None else 0
        
        if term:
            vocab_list.append({
                "id": page_id,
                "term": term,
                "type": term_type,
                "stage": stage,
                "count": count
            })
            
    return vocab_list

def select_vocab_for_generation(vocab_items):
    """Keeps all review items and limits only fresh stage 0 words."""
    review_items = [item for item in vocab_items if item["stage"] > 0]
    new_items = [item for item in vocab_items if item["stage"] == 0]
    selected_new_items = new_items[:NEW_WORD_DAILY_LIMIT]
    skipped_new_items = new_items[NEW_WORD_DAILY_LIMIT:]
    
    print(f"[selection] Review items due today: {len(review_items)}")
    print(f"[selection] New items due today: {len(new_items)}")
    print(f"[selection] New items selected today: {len(selected_new_items)}")
    
    if skipped_new_items:
        print(
            f"[selection] Skipped {len(skipped_new_items)} new items because the daily "
            f"new-word limit is {NEW_WORD_DAILY_LIMIT}."
        )
    
    return review_items + selected_new_items

def update_notion_word(page_id, current_stage, current_count):
    """Updates the word's Spaced Repetition data, Last Generated, and usage Count."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    today_dt = datetime.date.today()
    
    # Calculate next review date based on our interval mapping
    days_to_add = INTERVALS.get(current_stage, 30) # Default to 30 days if graduated
    next_review_date = (today_dt + datetime.timedelta(days=days_to_add)).isoformat()
    new_stage = current_stage + 1
    
    payload = {
        "properties": {
            "Review Stage": {"number": new_stage},
            "Next Review Date": {"date": {"start": next_review_date}},
            "Last Generated": {"date": {"start": today_dt.isoformat()}},
            "Count": {"number": current_count + 1}
        }
    }
    
    send_notion_request("patch", url, "Updating Notion review state", json=payload)

CLAUDE_MODEL = "claude-opus-5"
SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "vocab_system.md"
CLAUDE_TIMEOUT_SECONDS = 900

# Claude Code's own system prompt describes a coding agent. This overrides that framing
# without repeating the brief, which is far too long to pass as a command line argument.
SYSTEM_PROMPT_SUFFIX = (
    "You are writing a spoken podcast script, not doing software work. "
    "Do not use any tools, do not read or write files, and do not explain yourself. "
    "Reply with the finished script text and nothing else."
)

# A finished lesson for even a single word runs well past this. The floor exists to catch
# a refusal or an error string, both of which are short.
MIN_SCRIPT_WORDS = 150

# Phrases that mean the run failed rather than taught anything. Deliberately specific:
# a plain word like "error" would fire on a legitimate lesson about the word error.
FAILURE_MARKERS = (
    "invalid api key",
    "authentication_failed",
    "please run /login",
    "login expired",
    "credit balance",
    "rate_limit",
    "oauth token",
    "claude code",
)


def claude_executable():
    """Locates the Claude Code CLI, which npm installs as claude.cmd on Windows."""
    for candidate in ("claude", "claude.cmd", "claude.exe"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved

    raise FileNotFoundError(
        "Claude Code CLI not found on PATH. Install it with: "
        "npm install -g @anthropic-ai/claude-code"
    )


def parse_claude_result(raw_stdout):
    """Pulls the script out of the --output-format json envelope.

    Claude Code reports some in-run failures, a missing credential among them, as an
    ordinary result on stdout instead of a non-zero exit. Checking is_error here is what
    stops an authentication error being narrated into tomorrow's episode.
    """
    try:
        envelope = json.loads(raw_stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Claude Code did not return JSON. First 500 characters: {raw_stdout[:500]}"
        ) from error

    if envelope.get("is_error"):
        raise RuntimeError(
            f"Claude Code returned an error result: {str(envelope.get('result'))[:500]}"
        )

    script_text = (envelope.get("result") or "").strip()
    if not script_text:
        raise RuntimeError("Claude Code returned an empty result.")

    estimated_cost = envelope.get("total_cost_usd")
    if estimated_cost is not None:
        print(
            f"[claude] Equivalent API cost estimate: ${estimated_cost:.4f}. "
            f"The run itself draws on the subscription, not API credit."
        )

    return script_text


def validate_script(script_text, vocab_items):
    """Refuses to publish anything that does not look like a finished lesson.

    The MP3 and the feed entry are committed without anyone reading them first, so a bad
    generation is not a failed run, it is a bad episode in a public podcast feed. Every
    check here is a hard failure, which leaves Notion untouched and the words still due.
    """
    problems = []

    word_count = len(script_text.split())
    if word_count < MIN_SCRIPT_WORDS:
        problems.append(f"only {word_count} words, expected at least {MIN_SCRIPT_WORDS}")

    lowered = script_text.lower()
    taught_terms = {item["term"].lower() for item in vocab_items}
    for marker in FAILURE_MARKERS:
        # Skip a marker that is genuinely one of today's terms, however unlikely.
        if marker in lowered and marker not in taught_terms:
            problems.append(f"contains the failure marker {marker!r}")

    missing_terms = [
        item["term"] for item in vocab_items
        if item["term"].lower().split()[0] not in lowered
    ]
    if missing_terms:
        problems.append(f"never mentions: {', '.join(missing_terms)}")

    if problems:
        raise ValueError("Generated script failed validation: " + "; ".join(problems))

    print(
        f"[validate] Script accepted: {word_count} words, "
        f"all {len(vocab_items)} terms present."
    )


def generate_podcast_script(vocab_items):
    """Sends the daily vocab list to Claude Code to write an optimized podcast script."""

    # Format the data cleanly so the model can interpret the JSON structure easily.
    formatted_list = [
        {"term": item["term"], "type": item["type"], "stage": item["stage"]}
        for item in vocab_items
    ]

    if not SYSTEM_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"{SYSTEM_PROMPT_PATH} is missing. It holds the lesson brief that used to "
            f"live inside this file."
        )

    # The brief travels on stdin, not as an argument. There is no --append-system-prompt-file
    # flag, and the brief is over 4KB, which on Windows routes through cmd.exe and its 8191
    # character command line limit. Piping sidesteps that and behaves the same on the runner.
    # The short suffix below is what keeps Claude Code's coding-agent framing from adding a
    # preamble. dontAsk plus --permission-prompts none means a scheduled run can never block
    # waiting for an approval nobody is there to give.
    command = [
        claude_executable(),
        "-p", "Write today's episode from the brief and vocabulary list that follow.",
        "--model", CLAUDE_MODEL,
        "--append-system-prompt", SYSTEM_PROMPT_SUFFIX,
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--output-format", "json",
    ]

    piped_input = (
        SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
        + "\n\nToday's vocabulary list:\n"
        + json.dumps(formatted_list, ensure_ascii=False)
    )

    print(f"🤖 Invoking {CLAUDE_MODEL} through Claude Code to draft the daily audio script...")

    def generate_once():
        completed = subprocess.run(
            command,
            input=piped_input,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=CLAUDE_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"Claude Code exited with {completed.returncode}: {detail[:500]}"
            )
        return parse_claude_result(completed.stdout)

    return run_with_retries("Generating podcast script", generate_once)


async def convert_text_to_mp3(ssml_script, output_filename="podcast.mp3"):
    """Uses edge-tts to transform the generated script into a high-quality human MP3 voice."""
    print("🎙️ Synthesizing human speech audio track...")
    
    # We use a natural neural voice. We can change this to 'en-GB-SoniaNeural' or any Microsoft Edge voice.
    voice = "en-US-JennyNeural" 
    
    # edge-tts natively supports SSML input strings
    async def synthesize_once():
        communicate = edge_tts.Communicate(ssml_script, voice)
        await communicate.save(output_filename)
    
    await run_async_with_retries("Synthesizing speech with Edge TTS", synthesize_once)
    print(f"🎉 Success! Audio saved perfectly to {output_filename}")

import json
import email.utils
import time

def generate_rss_feed(new_mp3_name, script_text):
    """Logs the new episode and rebuilds a standard podcast-compliant feed.xml file."""
    json_log = "episodes.json"
    rss_file = "feed.xml"
    
    # 1. Load or initialize your episode history tracker
    if os.path.exists(json_log):
        with open(json_log, "r", encoding="utf-8") as f:
            episodes = json.load(f)
    else:
        episodes = []

    # 2. Add today's episode metadata
    today_str = datetime.date.today().isoformat()
    rfc_date = email.utils.formatdate(time.time(), usegmt=True)
    
    # Clean up script text for XML safety
    clean_summary = script_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    # Check if we already logged today to prevent duplicate runs adding duplicate entries
    if not any(ep["date"] == today_str for ep in episodes):
        episodes.insert(0, {
            "date": today_str,
            "rfc_date": rfc_date,
            "mp3": new_mp3_name,
            "summary": clean_summary[:300] + "..." # Snippet for the episode notes
        })
        with open(json_log, "w", encoding="utf-8") as f:
            json.dump(episodes, f, indent=4)

    # 3. Build the XML Feed String from scratch
    github_user = "Subram-skcet" 
    base_url = f"https://{github_user}.github.io/Vocab-Agent-Bot"

    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>My Daily AI Vocab Digest</title>
    <link>{base_url}</link>
    <language>en-us</language>
    <itunes:author>Vocab Agent</itunes:author>
    <description>Custom personalized daily spaced repetition audio lessons</description>
    <itunes:summary>Custom personalized daily spaced repetition audio lessons.</itunes:summary>
    <itunes:explicit>no</itunes:explicit>
    <itunes:category text="Education"/>
    <itunes:image href="https://picsum.photos/3000/3000"/>
    """

    for ep in episodes:
        xml_content += f"""
    <item>
      <title>Daily Vocab Lesson: {ep['date']}</title>
      <description>{ep['summary']}</description>
      <pubDate>{ep['rfc_date']}</pubDate>
      <guid isPermaLink="true">{base_url}/{ep['mp3']}</guid>
      <enclosure url="{base_url}/{ep['mp3']}" type="audio/mpeg" length="1024000"/>
    </item>"""

    xml_content += """
  </channel>
</rss>"""

    with open(rss_file, "w", encoding="utf-8") as f:
        f.write(xml_content.strip())
    print("📰 Podcast RSS Feed (feed.xml) successfully updated!")

async def main():
    print("🚀 Starting Daily Vocab Agent Engine...")
    
    # 1. Fetch due words from Notion
    vocab_items = get_todays_vocab()
    if not vocab_items:
        print("📭 No words due for review today. Add some words via your Telegram Bot!")
        return
        
    print(f"📚 Found {len(vocab_items)} terms to process for today's episode.")
    
    selected_vocab_items = select_vocab_for_generation(vocab_items)
    if not selected_vocab_items:
        print("No words selected for today's episode.")
        return
    
    # 2. Ask model to write the script
    ssml_script = generate_podcast_script(selected_vocab_items)
    
    # Clean up any accidental markdown formatting if the model included it
    ssml_script = ssml_script.replace("```xml", "").replace("```", "").strip()
    
    # 2.5 Refuse to publish a bad generation. Everything past this point writes to the
    # public feed and advances the Notion schedule, and neither is easy to walk back.
    validate_script(ssml_script, selected_vocab_items)
    
    # 3. Convert script text to an audio file
    mp3_filename = f"vocab_{datetime.date.today().isoformat()}.mp3"
    await convert_text_to_mp3(ssml_script, mp3_filename)

    # 3.5 Generate the updated Podcast RSS XML file
    generate_rss_feed(mp3_filename, ssml_script)
    
    # 4. Push updates back to Notion to advance the spaced-repetition schedules
    print("🔄 Updating Spaced Repetition states in Notion...")
    for item in selected_vocab_items:
        update_notion_word(item["id"], item["stage"], item["count"])
        
    print("🏁 Today's generation execution finished successfully!")

if __name__ == "__main__":
    asyncio.run(main())
