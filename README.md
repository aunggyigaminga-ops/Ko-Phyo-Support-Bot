# Telegram Anti-Spam Moderation Bot

ဒီ Bot က Telegram Group / Public Channel ရဲ့ Linked Discussion Group ထဲမှာ—

- Channel link နဲ့ URL ကြော်ငြာ ဖျက်ပေးခြင်း
- Spam / Flood တားခြင်း
- Scam / Phishing စကားလုံးများ စစ်ခြင်း
- Warning ၃ ကြိမ်ပြည့်လျှင် Mute လုပ်ခြင်း
- New member CAPTCHA verification
- Private Chat မှာ Owner contact button ပြခြင်း
- User message ကို Owner ဆီ Forward လုပ်ခြင်း

တို့ကို လုပ်ပေးပါတယ်။

Telegram official Sponsored Ads ကိုတော့ ဒီ Bot က မဖျက်ပါ။

## Installation

```bash
git clone YOUR_GITHUB_REPOSITORY_URL
cd telegram-moderation-bot

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
