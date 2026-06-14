import os
import datetime
import asyncio
import time
import json
import email.utils
import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
import edge_tts

# Load environment variables
load_dotenv()
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

# Initialize the modern Gemini client
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

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
NEW_WORD_DAILY_LIMIT = 10

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

def generate_podcast_script(vocab_items):
    """Sends the daily vocab list to Gemini to write an optimized SSML podcast script."""
    
    # Format the data cleanly so Gemini can interpret the JSON structure easily
    formatted_list = [{"term": item["term"], "type": item["type"], "stage": item["stage"]} for item in vocab_items]
    
    system_instruction = """
    You are an expert, friendly English language teacher and audio scriptwriter.
    I will provide a JSON list of vocabulary items with term, type, and stage.
    Write one cohesive, natural spoken lesson for a daily vocabulary podcast.

    Output rules:
    - Output only the final script text.
    - Do not use markdown, bullet points, numbered lists, XML, SSML tags, section labels, or code fences.
    - Do not mention JSON, Notion, Gemini, stages, or internal rules.
    - Do not invent extra vocabulary items. Teach only the terms provided.
    - Keep the tone warm, direct, and conversational, as if speaking to one learner.
    - Keep explanations short enough for audio, but useful enough to remember.

    Content rules by item type:
    - Word: Do not give a pronunciation guide. Start with the base word and a plain English root meaning. Then explore its word family. Include the base word plus only the most common related forms that a learner is likely to hear or use in everyday English. Skip rare, archaic, highly technical, awkward, or forced derivatives. For each selected form, clearly state its part of speech, such as noun, verb, adjective, adverb, gerund, or phrase. Explain that form briefly and give exactly two short, natural sample sentences for it. Include one memory cue for the base or root meaning only. Do not add a separate memory cue for every family member.
    - Phrasal Verb: If it has multiple meanings, include only the most common meanings. For each common meaning, explain the meaning, give one or two realistic situations where someone would use it, and include natural example sentences. Add one memory cue based on the literal image, verb plus particle logic, origin, or meaning pattern.
    - Idiom: Explain the literal visual image first, then the real meaning. Give one natural example sentence. Add one memory cue based on the image, origin, or meaning logic.
    - Phrase: Explain what the phrase means and when someone would say it. Give exactly three real world examples. Add one memory cue based on the literal meaning, origin, word parts, or situation where the phrase naturally fits.

    Memory cue rules:
    - Every vocabulary item needs a quick memory cue, except that Word items need only one cue for the base or root meaning.
    - Memory cues must explain why the expression means what it means.
    - Prefer origin, literal image, word dissection, root meaning, or meaning logic.
    - Do not tell the learner to memorize by repetition, mugging up, or brute force.
    - Example of a good cue: monotonous comes from mono and tone, so it suggests one unchanged tone, which helps you connect it with something boring because it does not vary.

    Review and new learning rules:
    - Items with stage greater than 0 are review items.
    - For each review item, ask the learner to recall the meaning before explaining it again.
    - After a recall question, insert exactly this pause: ... ... ... ...
    - Then give a concise reminder that still follows the item specific rules above.
    - Items with stage 0 are new items.
    - For new items, teach the meaning clearly first, then examples, then the memory cue.

    Audio pacing rules:
    - Use normal commas and periods for natural breathing.
    - Use a single ellipsis (...) only when a short pause improves the spoken rhythm.
    - Use exactly four spaced ellipses (... ... ... ...) only for recall pauses.
    - Avoid long, complex sentences. Prefer clear spoken sentences.
    - Do not overuse dramatic pauses.

    Script structure:
    - Start with one brief sentence that says how many terms are in today's lesson and names them.
    - Teach all review items first, if any.
    - Then teach all new items, if any.
    - End with one short outro sentence saying today's session is complete.

    Formatting restrictions:
    - Avoid special symbols that sound awkward in text to speech.
    - Do not use hyphens, forward slashes, backward slashes, asterisks, hashtags, square brackets, or emojis.
    - Parentheses are allowed only for part of speech labels or word part explanations.
    - Use only plain text with standard punctuation.
    """

    print("🤖 Invoking Gemini to draft the daily audio script...")
    
    # The modern SDK handles configuration parameters via a specific configuration object
    def generate_once():
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=str(formatted_list),
            config=types.GenerateContentConfig(
                system_instruction=system_instruction
            )
        )
        if not response.text:
            raise ValueError("Gemini returned an empty script.")
        return response
    
    response = run_with_retries("Generating podcast script with Gemini", generate_once)
    
    return response.text

async def convert_text_to_mp3(ssml_script, output_filename="podcast.mp3"):
    """Uses edge-tts to transform Gemini's SSML script into a high-quality human MP3 voice."""
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
    <description>Custom personalized daily spaced repetition audio lessons generated by Gemini.</description>
    <itunes:summary>Custom personalized daily spaced repetition audio lessons generated by Gemini.</itunes:summary>
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
    
    # 2. Ask Gemini to write the script
    ssml_script = generate_podcast_script(selected_vocab_items)
    
    # Clean up any accidental markdown formatting if Gemini included it
    ssml_script = ssml_script.replace("```xml", "").replace("```", "").strip()
    
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
