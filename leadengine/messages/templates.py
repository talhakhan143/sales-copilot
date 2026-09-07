"""Template fallback — jab Claude CLI na chale ya limit lag jaye.

Copy utni zinda nahi hoti jitni AI wali, lekin pipeline kabhi rukti nahi
aur numbers phir bhi asli hote hain.
"""

from __future__ import annotations


def _first_problem(scored: dict) -> tuple[str, str]:
    points = scored.get("pain_points") or []
    if not points:
        return "", ""
    return points[0]["title"], points[0]["detail"]


def _stat(lead: dict) -> str:
    reviews = lead.get("review_count")
    rating = lead.get("rating")
    if reviews and rating:
        return str(reviews) + " reviews aur " + str(rating) + "★"
    if reviews:
        return str(reviews) + " reviews"
    return ""


def _stat_en(lead: dict) -> str:
    reviews = lead.get("review_count")
    rating = lead.get("rating")
    if reviews and rating:
        return str(reviews) + " reviews at " + str(rating) + " stars"
    if reviews:
        return str(reviews) + " reviews"
    return ""


def build(lead: dict, audit: dict, scored: dict, language: str = "en") -> dict:
    name = lead.get("name", "there")
    city = (lead.get("city") or lead.get("search_location") or "").title()
    niche = (lead.get("search_niche") or lead.get("category") or "business").lower()
    is_website_deal = scored["money"]["deal_type"] == "WEBSITE"
    _, problem_detail = _first_problem(scored)
    rank = scored.get("local_rank")

    if language == "roman_urdu":
        stat = _stat(lead)
        hook = (stat + " — ") if stat else ""
        offer = "ek proper website" if is_website_deal else "aapki website theek"
        e1 = (
            hook + name + " Google pe " + (("#" + str(rank) + " pe aata hai. ") if rank else "achha chal raha hai. ")
            + ("Lekin aapki koi website nahi hai. " if is_website_deal else "Lekin website customers ko rok nahi pa rahi. ")
            + "Jo log click kar ke aapke baare me parhna chahte hain, unhe kuch milta hi nahi — "
            "aur wo competitor ke paas chale jate hain.\n\n"
            "Main " + offer + " karta hun. Agar chahen to main aapko 2 minute me dikha sakta hun "
            "ke abhi kya miss ho raha hai. Bas \"haan\" likh dijiye."
        )
        e2 = (
            problem_detail[:220] + "\n\n"
            "Main " + city + " ke " + niche + " businesses ke liye yehi kaam karta hun. "
            "Ek 10 minute ki call rakh lein? Bata dunga ke kya karna chahiye — chahe aap "
            "khud karwa lein."
        )
        return {
            "email": [
                {"subject": name + " ki online presence", "body": e1},
                {"subject": "ek cheez jo " + name + " ke customers miss kar rahe hain", "body": e2},
            ],
            "instagram": [
                (hook + "aapka kaam clearly achha hai. Lekin "
                 + ("website hi nahi hai" if is_website_deal else "website customers ko convert nahi kar rahi")
                 + ". Main websites banata hun " + city + " ke businesses ke liye. "
                 "Dikhaun ke abhi kya miss ho raha hai?"),
                (name + " " + ("Google pe #" + str(rank) + " pe hai" if rank else "achha chal raha hai")
                 + " — magar " + ("koi website nahi" if is_website_deal else "site theek nahi")
                 + ". Ek chhoti si cheez hai jo mahine ke kai customers wapas la sakti hai. "
                 "Batau?"),
            ],
            "sms": [
                ("Assalam o alaikum, main web designer hun. " + name + " ki Google listing dekhi — "
                 + (stat + ", " if stat else "") + ("website nahi mili" if is_website_deal else "website me kuch masle hain")
                 + ". Aik chhota sa fix bohat farq daal sakta hai. Interested hon to reply kar dein."),
                (name + " ke liye ek suggestion tha — " + ("website ka" if is_website_deal else "website theek karne ka")
                 + ". 10 min ki call kar lein? Main hun " + city + " ke local businesses ke saath kaam karta hun."),
            ],
        }

    if language == "urdu":
        stat = _stat(lead)
        return {
            "email": [
                {
                    "subject": name + " کی آن لائن موجودگی",
                    "body": (
                        (stat + " — " if stat else "")
                        + name + " گوگل پر اچھا چل رہا ہے، لیکن "
                        + ("آپ کی کوئی ویب سائٹ نہیں ہے۔ " if is_website_deal else "ویب سائٹ گاہکوں کو روک نہیں پا رہی۔ ")
                        + "جو لوگ کلک کر کے آپ کے بارے میں پڑھنا چاہتے ہیں، انہیں کچھ نہیں ملتا۔\n\n"
                        "میں مقامی کاروبار کے لیے ویب سائٹس بناتا ہوں۔ اگر اجازت ہو تو دو منٹ میں "
                        "دکھا سکتا ہوں کہ ابھی کیا مِس ہو رہا ہے۔"
                    ),
                },
                {"subject": "ایک چیز جو " + name + " کے گاہک مِس کر رہے ہیں", "body": problem_detail[:260]},
            ],
            "instagram": [
                (name + " کا کام اچھا ہے لیکن "
                 + ("ویب سائٹ ہی نہیں" if is_website_deal else "ویب سائٹ ٹھیک نہیں")
                 + "۔ دکھاؤں کہ کیا مِس ہو رہا ہے؟"),
                ((stat + "۔ " if stat else "") + "ایک چھوٹی سی تبدیلی مہینے کے کئی گاہک واپس لا سکتی ہے۔"),
            ],
            "sms": [
                ("السلام علیکم۔ میں ویب ڈیزائنر ہوں۔ " + name + " کی گوگل لسٹنگ دیکھی — "
                 + ("ویب سائٹ نہیں ملی" if is_website_deal else "ویب سائٹ میں کچھ مسائل ہیں") + "۔ بات کر سکتے ہیں؟"),
                (name + " کے لیے ایک تجویز تھی۔ دس منٹ کی کال ہو سکتی ہے؟"),
            ],
        }

    # -- English ---------------------------------------------------------
    # Teen alag skeleton, lead_key ke hash se chunte hain. Ek hi shape sab
    # leads pe lagti thi to poori list template lagti thi.
    stat = _stat_en(lead)
    shape = _shape_for(lead)
    offer = (
        "I already built a homepage for " + name + "."
        if is_website_deal
        else "I wrote up what is costing you customers."
    )
    ask = (
        "Want me to send it over? Free to look at, and you only pay if you keep it."
        if is_website_deal
        else "Want me to send it across? No charge, nothing owed."
    )

    if shape == 0:
        # customer-moment opener
        lead_in = (
            "Somebody searches \"" + niche + " near me\", finds " + name
            + ", and taps through to check hours or prices. "
            + (
                "There is nothing on the other side of that tap."
                if is_website_deal
                else "What loads next is doing you no favours."
            )
        )
        subject = "what happens after someone taps " + name.lower()
    elif shape == 1:
        # single-detail opener
        lead_in = (
            (problem_detail[:180].rstrip() if problem_detail else
             "The Google listing for " + name + " has no website behind it.")
        )
        subject = "one thing on " + name.lower()
    else:
        # direct-offer opener
        lead_in = offer + " Nothing was asked for and nothing is owed."
        subject = (
            ("a homepage for " + name.lower() + ", ready when you are")
            if is_website_deal
            else ("what is costing " + name.lower() + " customers")
        )

    rank_line = (
        " You sit at #" + str(rank) + " for \"" + niche + "\" in " + city + ", so the "
        "traffic is already there."
        if rank
        else ""
    )

    e1 = lead_in + rank_line + "\n\n" + offer + " " + ask + "\n\n" + sender_line(lead)
    e2 = (
        (problem_detail[:200].rstrip() if problem_detail else lead_in)
        + "\n\n"
        + (
            "Happy to build the page first and let you look before you decide anything. "
            "No cost either way."
            if is_website_deal
            else "Happy to write the whole thing up and send it over. No cost either way."
        )
        + "\n\n"
        + sender_line(lead)
    )

    return {
        "email": [
            {"subject": subject, "body": e1},
            {
                "subject": (
                    "no charge to look at this"
                    if shape != 2
                    else "about " + name.lower()
                ),
                "body": e2,
            },
        ],
        "instagram": [
            lead_in[:200] + " " + offer + " " + ask,
            (
                offer
                + " You look at it first and decide after \u2014 nothing owed if it is not for you."
                + ((" " + stat + " and no site to point them at is a strange place to be.") if (stat and is_website_deal) else "")
            )[:320],
        ],
        "sms": [
            (
                _sms_intro(lead) + offer + " " + ask
            )[:240],
            (
                _sms_intro(lead)
                + (
                    "built you a homepage already and would rather you just look at it than "
                    "take my word for it. OK to send?"
                    if is_website_deal
                    else "put together a short breakdown of what is losing you customers. OK to send?"
                )
            )[:240],
        ],
    }


def _shape_for(lead: dict) -> int:
    """Stable 0-2, taake ek hi lead hamesha ek hi shape le magar list bhar me teeno chalein."""
    import hashlib

    key = str(lead.get("feature_id") or lead.get("gmb_url") or lead.get("name") or "")
    return int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], 16) % 3


def _sender_name() -> str:
    from leadengine.settings import load_config

    return str(load_config("profile.json").get("your_name") or "").strip()


def sender_line(lead: dict) -> str:
    return _sender_name()


def _sms_intro(lead: dict) -> str:
    name = _sender_name()
    return (name + " here — ") if name else ""
