"""One lock for everything that talks to Google: the lead worker and the reviews worker take turns, so the
engine never runs two scrapes against Google at once (two Chromes from one IP double the chance of the
unusual-traffic page)."""
import threading

GOOGLE = threading.Lock()
