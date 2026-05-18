import json

path = r'app\prompt.md'
with open(path, 'r', encoding='utf-8') as f:
    data = json.load(f)

p = data['prompt']

language_section = (
    "[Language Detection]\n"
    "This assistant operates in multilingual mode. Always detect the language the caller is\n"
    "speaking and respond entirely in that same language for the rest of the call.\n"
    "\n"
    "- If the caller speaks English \u2192 respond in English.\n"
    "- If the caller speaks Portuguese \u2192 respond in Portuguese.\n"
    "- If the caller speaks Spanish, French, Hindi, or any other language \u2192 respond in that language.\n"
    "- Once you detect the caller's language, keep using it consistently \u2014 do not switch back to English.\n"
    "- If the caller switches language mid-call, follow them and switch too.\n"
    "- All scripts, confirmations, booking summaries, and error messages must be delivered\n"
    "  in the caller's detected language, not in English.\n"
    "\n"
)

anchor = "[System Info]"
idx = p.find(anchor)
if idx >= 0:
    p = p[:idx] + language_section + p[idx:]
    print("Language section inserted before [System Info].")
else:
    p = language_section + p
    print("Anchor not found; prepended to prompt.")

data['prompt'] = p
with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4, ensure_ascii=False)
print(f"Saved. Prompt length: {len(p)}")
