# dryarapureddy-options

Pick any NSE F&O symbol (index or stock), an expiry, and a strike -- see the
CE (Call) and PE (Put) candlestick charts for that strike side by side.

## Setup (new GitHub repo + Streamlit Cloud deploy)

1. Create a new empty GitHub repo named `dryarapureddy-options` under your
   `rsireddy002` account (via github.com -> New repository -- don't
   initialize with a README).

2. On your PC, in a terminal, from the folder where you keep your other
   repos (e.g. `cd path\to\your\repos`):

   ```
   git clone https://github.com/rsireddy002/dryarapureddy-options.git
   ```

3. Copy `app.py`, `requirements.txt`, and the `.streamlit` folder from this
   delivered folder into that new `dryarapureddy-options` folder.

4. Rename `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and
   fill in your real `UPSTOX_ACCESS_TOKEN` (the same one you refresh daily
   for your other apps). This local file is only for testing on your PC --
   it's already covered by the usual `.gitignore` pattern, but double check
   it isn't committed.

5. Test locally:

   ```
   cd dryarapureddy-options
   streamlit run app.py
   ```

6. Commit and push:

   ```
   git add app.py requirements.txt
   git commit -m "Add CE/PE side-by-side option chart app"
   git push
   ```
   (Do not `git add` your `secrets.toml` -- secrets go into Streamlit Cloud's
   Settings -> Secrets instead, not into the repo.)

7. On share.streamlit.io, deploy a new app from `rsireddy002/dryarapureddy-options`,
   main branch, `app.py`.

8. In the new app's Settings -> Secrets, paste:

   ```
   APP_PASSWORD = "Garden@7948"
   UPSTOX_ACCESS_TOKEN = "your-real-token"
   ```

9. Open the app, enter the password, pick a symbol/expiry/strike, and confirm
   both CE and PE candlestick charts load side by side.

## Notes

- Symbol list: the 5 major indices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY,
  SENSEX) plus every NSE stock with listed F&O options, pulled live from
  Upstox's own instrument master and cached for 6 hours.
- Uses Upstox's Option Contract + Option Chain endpoints to list expiries and
  strikes, and the intraday historical-candle endpoint (same one your other
  apps use) for the actual CE/PE candles.
- Same password gate and token-in-secrets pattern as your other apps, so it
  fits into your existing "delete a letter in the token to disable" trick if
  you ever want to pause this one too.
