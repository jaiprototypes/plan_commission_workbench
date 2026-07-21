"""Build-time developer diagnostic email defaults.

The secret-bearing values are intentionally empty in source. The Windows release
workflow rewrites this module inside the build runner so packaged apps can send
support diagnostics without writing email secrets into git history.
"""

RECIPIENT = ""
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USERNAME = ""
SMTP_PASSWORD = ""
SENDER = ""
USE_SSL = False
USE_STARTTLS = True
AUTO_EMAIL_FAILURES = False
