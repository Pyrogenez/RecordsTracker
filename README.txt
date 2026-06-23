Records Tracker - Getting Started
==================================

What this is
------------
A local program that pulls your public records requests from the city's
online portal into a searchable database on YOUR computer. It does not
submit anything or change anything on the portal - it only READS.

You get:
  - A web page running on your own computer that lists every request,
    shows every message, and lets you chat with Claude about any record.
  - Automatic flagging of potential Chapter 119 (public records law)
    violations.
  - An Excel snapshot and a folder of every attachment the city has
    sent you.

Everything stays on your computer. The only things that leave your
computer are:
  - Your login, sent to the portal you already use (stpetefl.mycusthelp.com).
  - Record content you explicitly ask Claude to analyze, sent to
    Anthropic's API using YOUR API key.


First time setup
----------------
1. Unzip this folder somewhere permanent, like your Documents folder.
   (Do NOT keep it in Downloads - pick a real home for it.)

2. Double-click "Install.bat".

   It will:
     - Check that Python is installed on your computer. If not, it
       opens the download page. Grab Python 3.11 or newer and make
       sure you check "Add python.exe to PATH" in the installer.
     - Set up a private workspace inside this folder.
     - Install the pieces it needs from the internet.
     - Download the browser it uses to scrape (this is the slow step -
       be patient).
     - Ask you for:
         * Your portal login (the email + password you use at
           stpetefl.mycusthelp.com)
         * Your Anthropic API key, if you want the AI features.
           Get one at https://console.anthropic.com/ - you can skip
           this and add it later.

3. When Install.bat finishes, you're done with setup.


Day-to-day use
--------------
Double-click "Start.bat"
  Opens the web interface in your browser. This is the main way to
  use the program. Keep the black command window open while you're
  using it - closing that window stops the program.

  In the web interface you can:
    - See a Dashboard: how fast the city replies, which requests are
      overdue, and how many you file each month.
    - Browse, search, sort, and filter all your requests.
    - Open any request to read every message, OPEN/download the files
      the city sent, see an AI summary, and chat with Claude about it.
    - Run a Florida Chapter 119 compliance audit and review (or print)
      the flagged issues.
    - From the "Runs & Sync" page, start a scrape or AI analysis
      WITHOUT touching the command line, and see your run history.
    - Switch between light and dark mode (button at the bottom-left).

Double-click "Scrape.bat"
  Quickly checks the portal for updates on your OPEN requests and any
  new requests that have appeared. Fast. Run this whenever.

Double-click "FullScrape.bat"
  Re-checks EVERY request you've ever made, including closed ones.
  Use this the very first time, or if you want to refresh everything.
  Can take a while.


Why scraping is slow (on purpose)
---------------------------------
The city can see every time a record is opened. To avoid tripping
their "automated scraper" alarms, the program pauses between each
record for a random, variable, human-sized amount of time - as if
you were reading each page before clicking the next one. Most
pauses are 20-75 seconds, with occasional 2-5 minute breaks mixed
in.

This means a full scrape of hundreds of records takes HOURS, not
minutes. That is the point. Leave it running; do not try to speed
it up unless you really know what you are doing (the knobs live in
config.json under "human_delay").


Where your stuff lives
----------------------
  data\records.db            - The database of all your requests.
  data\downloads\            - Every attachment the city has sent you,
                               organized by request.
  data\records_analysis.xlsx - An Excel snapshot, refreshed each scrape.
  credentials.json           - Your portal login (do not share).
  config.json                - Settings, including your API key.
  logs\                      - Daily log files, useful if something goes wrong.


Using this with Claude Cowork
------------------------------
If you have the Claude Cowork desktop app, Claude can run the scraper
for you, read your logs, answer questions about your records, and
apply updates - all from chat. See COWORK_QUICKSTART.txt in this
folder for 4 short steps to get set up. Highly recommended.


Getting updates
---------------
If you receive a new version as a file named something like
"update-1.1.0.zip":

  1. Save the zip file into THIS folder (the same folder as Update.bat).
  2. Double-click "Update.bat".

The update only changes the program. Your database, downloads, login,
and settings are preserved.


Troubleshooting
---------------
"Python is NOT installed"
  Install Python from python.org. Check the "Add python.exe to PATH"
  box during install. Then run Install.bat again.

"Address already in use"
  Another program is using port 5000. Close it, or change the port
  in Start.bat (add --port 8765 after server.py).

"Could not locate login fields"
  The portal's web page probably changed. Message the person who sent
  you this program - they can ship an update.

Something else is broken
  Look at the most recent log in the logs\ folder and send it to the
  person who gave you this program.


Privacy reminder
----------------
credentials.json contains your portal password in plain text. Don't
share that file, don't put this folder on a shared drive, and don't
commit it to git if you happen to use git.
