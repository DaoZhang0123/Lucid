# Lucid Demo Gallery

This page collects Lucid demo videos and real-world usage scenarios in one place.

> Demo model note: by default, demos are run with Opus 4.7 unless explicitly labeled otherwise.

## Teams precise click (GPT-5.5)

This demo is specifically tested with GPT-5.5 for Teams click precision.

https://github.com/user-attachments/assets/edc242de-20af-43ab-bdb7-66bb29f6572c

## Teams click test (Gemini 3.5 Flash)

This demo is tested with Gemini 3.5 Flash for Teams click behavior.

https://github.com/user-attachments/assets/74baba49-542d-4791-bc93-f1b08fe61eb3

## Accessibility: Voice-first desktop control

For people who can speak but have limited hand mobility, Lucid can turn many computer tasks into a spoken command.

https://github.com/user-attachments/assets/a21a9b5d-f73a-4f6b-a0f9-1dec5a41a94f

```
Voice command: "..."
        ↓
Lucid: transcribes speech with Whisper
        → sees the current app UI
        → clicks, types, and navigates on the real desktop client
        → completes the requested task end-to-end
```

## Voice + precise click (Teams)

Voice input + precise clicking. Hold the spacebar, speak the request; Lucid transcribes it locally with Whisper, then drives Teams entirely by vision (no Teams API).

https://github.com/user-attachments/assets/b32124f2-0964-457a-9e61-88eba614f9a0

```
Voice (hold spacebar):  "Send greetings to Dao Zhang in Teams."
          ↓
Lucid:   transcribe → launch_app("Microsoft Teams")
          → click(search) → type("Dao Zhang") → click(top result)
          → click(chat input) → type("Hi Dao, just sending greetings — hope you're well!") → key("enter")
          → "Done. Greetings sent in Teams."
```

## Workplace: PowerPoint draft end-to-end

Lucid hears the request, opens PowerPoint and drafts the deck end-to-end.

https://github.com/user-attachments/assets/12d4a7d8-33c1-4579-a65b-8b26f6180869

```
Voice / chat:  "Create a PowerPoint about Microsoft's latest AI strategy."
          ↓
Lucid:   launch_app("PowerPoint") → New blank presentation
          → outline slides (title, pillars, Copilot, Azure AI, roadmap, summary)
          → click(title) → type(…) → add_slide → type bullets → repeat
          → "Done. Draft deck ready in PowerPoint."
```

## Chinese workflow: voice call via WeChat

Voice input drives WeChat entirely by vision (no WeChat API).

https://github.com/user-attachments/assets/9bb5b9e9-9437-45f5-be00-9ae3dfea782c

```
Voice (hold spacebar):  "Call dad"
          ↓
Lucid:   transcribe → launch_app("WeChat")
          → find("dad") → click(matched contact)
          → click(voice call button) → wait for connection
          → "Done. Voice call to dad started in WeChat."
```

## Chinese workflow: desktop doc → PPT → send back

Lucid reads a document on the desktop, generates a PowerPoint deck from it, and sends it back through chat.

https://github.com/user-attachments/assets/7841cec9-58bc-49fd-b87a-5a0a27529fd6

```
Voice / chat:  "There's a document on my desktop. Make a PPT based on it and send it back to me."
          ↓
Lucid:   scan desktop → locate source doc → read content
          → launch_app("PowerPoint") → fill slides per outline
          → save as .pptx → back to chat → send as attachment
          → "Done. PPT generated and sent."
```

## Language entry points

- English: [README.md](README.md)
- 简体中文: [README.zh-CN.md](README.zh-CN.md)
- Français: [README.fr-FR.md](README.fr-FR.md)
