# Privacy Policy — Clip Automation Pipeline

**Effective Date:** November 22, 2026

## Overview

Clip Automation Pipeline is a locally hosted tool used to record livestreams, extract highlight clips, and upload YouTube Shorts to authorized YouTube channels.

## Data Accessed via Google / YouTube API

When you connect a Google/YouTube account, the application requests OAuth access solely to:

- View basic YouTube channel information (channel ID and title) to identify connected accounts.
- Upload video drafts (YouTube Shorts) to your selected YouTube channel on your behalf.

## Data Storage and Security

- OAuth access and refresh tokens are encrypted using a local Fernet key (`TOKEN_ENCRYPTION_KEY`) and stored only in the local SQLite database on your machine.
- Media files and clip metadata remain on your local filesystem.
- No personal data, tokens, or video content are transmitted to external servers other than official Google/YouTube APIs for authentication and uploading.
- We do not sell, rent, or share user data with third parties.

## Revoking Access

You can disconnect an account in the application or revoke OAuth access at any time from your Google Account permissions page: https://myaccount.google.com/permissions
