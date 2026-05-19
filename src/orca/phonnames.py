# Orca
#
# Copyright 2006-2008 Sun Microsystems Inc.
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the
# Free Software Foundation, Inc., Franklin Street, Fifth Floor,
# Boston MA  02110-1301 USA.

"""Dictionary of phonetic names for letters of the alphabet."""

from .orca_i18n import _  # pylint: disable=import-error

# Translators: this is a structure to assist in the generation of
# spoken military-style spelling.  For example, 'abc' becomes 'alpha
# bravo charlie'.
#
# It is a simple structure that consists of pairs of
#
# letter : word(s)
#
# where the letter and word(s) are separate by colons and each
# pair is separated by commas.  For example, we see:
#
# a : alpha, b : bravo, c : charlie,
#
# And so on.  The complete set should consist of all the letters from
# the alphabet for your language paired with the common
# military/phonetic word(s) used to describe that letter.
#
# The Wikipedia entry
# http://en.wikipedia.org/wiki/NATO_phonetic_alphabet has a few
# interesting tidbits about local conventions in the sections
# "Additions in German, Danish and Norwegian" and "Variants".
#
__english_phonlist = (
    "a : alpha, b : bravo, c : charlie, "
    "d : delta, e : echo, f : foxtrot, "
    "g : golf, h : hotel, i : india, "
    "j : juliet, k : kilo, l : lima, "
    "m : mike, n : november, o : oscar, "
    "p : papa, q : quebec, r : romeo, "
    "s : sierra, t : tango, u : uniform, "
    "v : victor, w : whiskey, x : xray, "
    "y : yankee, z : zulu"
)

__phonlist = _(__english_phonlist)


def __parse_phonlist(phonlist):
    """Build a {letter: phonetic} dict from a comma-separated 'l : word' string.

    Returns the dict or None if any pair is malformed.
    """

    parsed = {}
    for pair in phonlist.split(","):
        parts = pair.split(":")
        if len(parts) != 2:
            return None
        letter = parts[0].strip()
        word = parts[1].strip()
        if not letter or not word:
            return None
        parsed[letter] = word
    return parsed


__phonnames = __parse_phonlist(__phonlist)
if __phonnames is None:
    # Translator produced a malformed table; fall back to the English
    # NATO alphabet so get_phonetic_name() still works rather than
    # taking the whole module (and Orca startup) down with it.
    __phonnames = __parse_phonlist(__english_phonlist) or {}


def get_phonetic_name(character):
    """Given a character, return its phonetic name (e.g. 'a' -> 'alpha')."""

    return __phonnames.get(character, character)
