"""Plain string constants used across the API, for editors that like names."""


class ParseMode:
    HTML = "html"
    MARKDOWN = "markdown"
    DISABLED = None


class ChatType:
    PRIVATE = "private"
    BOT = "bot"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    CHANNEL = "channel"


class ChatAction:
    TYPING = "typing"
    CANCEL = "cancel"
    UPLOAD_PHOTO = "upload_photo"
    UPLOAD_VIDEO = "upload_video"
    UPLOAD_VOICE = "upload_voice"
    UPLOAD_DOCUMENT = "upload_document"
    UPLOAD_VIDEO_NOTE = "upload_video_note"
    RECORD_VIDEO = "record_video"
    RECORD_VOICE = "record_voice"
    RECORD_VIDEO_NOTE = "record_video_note"
    CHOOSE_STICKER = "choose_sticker"
    FIND_LOCATION = "find_location"
    CHOOSE_CONTACT = "choose_contact"
    PLAYING = "playing"
