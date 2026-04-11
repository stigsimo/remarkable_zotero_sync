# zotero_sync

Sync your Zotero library with your reMarkable. PDFs go to the tablet, annotated PDFs come back to Zotero, and reMarkable highlights are converted into real PDF highlights that Zotero can interpret.

## What this tool does

This tool is built for a workflow where:

- Zotero is the main library and source of truth
- reMarkable is the reading and annotation device
- PDFs sync from Zotero to reMarkable
- annotated PDFs sync back from reMarkable into Zotero

It can sync either:

- a Zotero collection named `reMarkable Sync` and its subcollections
- or your whole Zotero library

## Features

- Syncs PDFs from Zotero to reMarkable
- Syncs annotated PDFs from reMarkable back into Zotero
- Converts reMarkable highlight overlays into real PDF highlights
- Uses Zotero paper titles on the reMarkable
- Maps Zotero collections and subcollections to reMarkable folders
- Creates the `/Zotero` folder and needed subfolders automatically
- Carries Zotero tags over to the reMarkable
- Adds author/year tags
- Replaces the Zotero PDF with the latest annotated version
- Skips unchanged items during repeat syncs

## Important notes

- Designed for and tested on Mac. Will not work as-is with Windows
- Tested with reMarkable Paper Pro
- Tested and working with reMarkable Paper Pro software version `3.26.0.68`
- Not yet confirmed to work with other reMarkable tablet models
- Likely will not work as-is on Windows
- May work on Linux, but Linux has not been tested
- Uses Developer Mode / SSH
- Zotero must be closed while the tool is running
- Future reMarkable software updates could break parts of the sync

## Setup

Read:

- `instructions.txt` for the full beginner-friendly guide
- `quick instructions.txt` for the short version
- `features and functions.txt` for an overview of what the tool does

## Basic usage

Run:

```bash
python3 main.py
