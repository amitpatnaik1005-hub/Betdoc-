import json
import re

with open(r'C:\Users\Amit Patnaik\.gemini\antigravity\brain\09ed08ce-4ba7-4dd5-a6d8-9dd6a601d440\.system_generated\steps\8711\content.md', 'r', encoding='utf-8', errors='ignore') as f:
    html_content = f.read()

# Look for text blocks in the JSON
# In newer ChatGPT shared links, text is often stored in 'parts' or 'text' nodes within Remix state.
messages = re.findall(r'\\"(?:text|parts)\\"\s*:\s*\[?\\"([^\\]+)\\"\]?', html_content)
with open('extracted_chat.txt', 'w', encoding='utf-8') as out:
    for msg in messages:
        if len(msg) > 20:
            out.write(msg + "\n\n")

if not messages:
    # Try another regex for escaped strings
    messages2 = re.findall(r'"(?:text|parts)"\s*:\s*\[?"([^"]+)"\]?', html_content)
    with open('extracted_chat.txt', 'a', encoding='utf-8') as out:
        for msg in messages2:
            if len(msg) > 20:
                out.write(msg + "\n\n")
