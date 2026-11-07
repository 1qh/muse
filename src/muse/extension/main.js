function chords() {
    var found = []
    var segment = curScore.firstSegment()
    while (segment) {
        var notes = segment.annotations
        for (var i = 0; i < notes.length; i++) {
            if (notes[i].type === Element.HARMONY && notes[i].track === 0) {
                found.push({ chord: notes[i], segment: segment, measure: segment.parent, index: found.length })
            }
        }
        segment = segment.next
    }
    return found
}

function start(measure) {
    // where a bar's beat-1 chord starts: its opening, and past any sign printed there
    var edge = opening(measure)
    var owned = measure.elements
    for (var m = 0; owned && m < owned.length; m++) {
        if (owned[m].type === Element.MARKER && owned[m].bbox && owned[m].pagePos.x - owned[m].offsetX <= measure.pagePos.x + 4) {  // at its bar's start before any shift clear of the bar number
            edge = Math.max(edge, owned[m].pagePos.x + owned[m].bbox.x + owned[m].bbox.width + 0.5)
        }
    }
    return edge
}

function opening(measure) {
    // where a bar's music starts: its left edge, or after the clef, key and time signature that open a line; a sign is chord room, never bar width
    var edge = measure.pagePos.x
    var before = measure.prevMeasure
    if (before && Math.abs(before.pagePos.y - measure.pagePos.y) < 0.5) {  // the barline closing the bar before, on the same line
        var last = before.lastSegment
        while (last && last.tick >= measure.firstSegment.tick - 1 && last.segmentType !== Segment.EndBarLine) last = last.prev
        var line = last ? last.elementAt(0) : null
        if (line && line.bbox) edge = line.pagePos.x + line.bbox.x + line.bbox.width
    }
    var segment = measure.firstSegment
    while (segment && segment.tick === measure.firstSegment.tick) {
        if (segment.segmentType === Segment.HeaderClef || segment.segmentType === Segment.Clef || segment.segmentType === Segment.KeySig || segment.segmentType === Segment.TimeSig) {
            var held = segment.elementAt(0)
            if (held && held.bbox) {
                edge = Math.max(edge, held.pagePos.x + held.bbox.x + held.bbox.width)
            }
        }
        segment = segment.next
    }
    return edge
}

function above(segment) {
    // the highest top of the music in a segment: notes, rests and tuplet brackets; text moves above the chords instead
    var top = 1e9
    var notes = segment.annotations
    for (var i = 0; i < notes.length; i++) {  // a fermata belongs to its note, so the chords clear it as they clear the note
        if (notes[i].type === Element.FERMATA && notes[i].bbox) top = Math.min(top, notes[i].pagePos.y + notes[i].bbox.y)
    }
    for (var track = 0; track < 4; track++) {
        var held = segment.elementAt(track)
        if (held && held.bbox) top = Math.min(top, held.pagePos.y + held.bbox.y)
        if (held && held.tuplet && held.tuplet.bbox) top = Math.min(top, held.tuplet.pagePos.y + held.tuplet.bbox.y)
    }
    return top
}

function opensLine(segment) {
    // a clef, key or time signature: chords start after these, so they never lift a chord row
    var kind = segment.segmentType
    return kind === Segment.HeaderClef || kind === Segment.Clef || kind === Segment.KeySig || kind === Segment.TimeSig
}

function lineKey(measure) {
    // a line is 1 system: its page and its height on that page
    var system = measure.parent
    var page = system && system.parent ? system.parent.pagenumber : 0
    return page + ":" + Math.round(measure.pagePos.y)
}

function lift(all) {
    // every chord on a line shares 1 baseline, high enough to clear what prints over the staff
    var lines = {}
    var segment = curScore.firstSegment()
    var clear = {}
    while (segment) {
        var key2 = lineKey(segment.parent)
        if (!opensLine(segment)) clear[key2] = Math.min(clear[key2] === undefined ? 1e9 : clear[key2], above(segment))  // no chord sits over the clef and signatures opening a line
        segment = segment.next
    }
    var lineTops = []
    var walk = curScore.firstSegment()
    while (walk) {
        var k = lineKey(walk.parent)
        if (lineTops.indexOf(k) < 0) lineTops.push(k)
        walk = walk.next
    }
    var spanners = curScore.spanners
    for (var s = 0; s < spanners.length; s++) {
        var parts = spanners[s].spannerSegments
        for (var t = 0; t < parts.length; t++) {
            var part = parts[t]
            if (!part.bbox || !part.parent || !part.parent.parent) continue
            var top = part.pagePos.y + part.bbox.y
            var system = part.parent
            if (!system.firstMeasure) continue
            var own = lineKey(system.firstMeasure)  // a spanner belongs to the line it is drawn in, never the next line down
            var staffTop = Number(own.split(":")[1])
            if (top < staffTop && top > staffTop - SPANNER_REACH) clear[own] = Math.min(clear[own] === undefined ? 1e9 : clear[own], top)  // farther up is a reading the fitting left stale, from the line above
        }
    }
    var m = curScore.firstMeasure
    while (m) {  // the staff's own top line is the lowest a chord row may come down to
        var mk = lineKey(m)
        clear[mk] = Math.min(clear[mk] === undefined ? 1e9 : clear[mk], m.pagePos.y)
        m = m.nextMeasure
    }
    for (var i = 0; i < all.length; i++) {
        var key = lineKey(all[i].measure)
        var bottom = all[i].chord.pagePos.y + all[i].chord.bbox.y + all[i].chord.bbox.height
        var need = clear[key] - GAP - bottom
        lines[key] = Math.min(lines[key] === undefined ? 1e9 : lines[key], need)
    }
    curScore.startCmd()
    for (var j = 0; j < all.length; j++) {  // every line's chords set on 1 baseline, raised or lowered, so an earlier lift never stays
        var line = lineKey(all[j].measure)
        all[j].chord.autoplace = false
        if (Math.abs(lines[line]) > 0.01) all[j].chord.offsetY = all[j].chord.offsetY + lines[line]
    }
    curScore.endCmd()
}

var GAP = 0.5  // spaces between a line's chords and the highest thing below them

var SPANNER_REACH = 10  // spaces a reading may lie from its own line or barline: a volta, hairpin or beat-1 chord sits within a few; farther is stale

var TEXT_SLACK = 1.0  // spaces of room text placement keeps: it reads positions inside the launch, which differ a little from what prints

var CLEAR = 0  // spaces of clear gap between 2 chords, passed by the caller

var BIAS = []

function left(item) { return item.chord.pagePos.x + item.chord.bbox.x }

var INK = []  // per chord, its printed ink width in spaces, measured by the caller from the PDF

function right(item) { return item.index < INK.length ? left(item) + INK[item.index] : item.chord.pagePos.x + item.chord.bbox.x + item.chord.bbox.width }

function seatStarts(all) {
    // every beat-1 chord at its barline, or after what opens its line
    var worst = 0
    for (var round = 0; round < 3; round++) {
        worst = 0
        curScore.startCmd()
        for (var i = 0; i < all.length; i++) {
            var item = all[i]
            if (item.segment.tick !== item.measure.firstSegment.tick) continue
            var gap = left(item) - start(item.measure) + (BIAS[i] || 0)
            if (Math.abs(gap) > SPANNER_REACH) continue  // a beat-1 chord sits within a few spaces of its barline: farther is a position the fitting left stale; the final checks judge where it prints
            if (Math.abs(gap) > 0.05) {
                item.chord.autoplace = false
                item.chord.offsetX = item.chord.offsetX - gap
                worst = Math.max(worst, Math.abs(gap))
            }
        }
        curScore.endCmd()
        if (worst <= 0.05) break
    }
    return worst
}

var BOX_PAD = 1.0  // spaces a sign's drawn box reaches past its glyph

function signsOf() {
    // per line, the left edge of every segno or coda box that is not the target opening the line
    var found = {}
    var m = curScore.firstMeasure
    while (m) {
        var owned = m.elements
        for (var i = 0; owned && i < owned.length; i++) {
            var e = owned[i]
            if ((e.type !== Element.MARKER && e.type !== Element.JUMP) || !e.bbox) continue
            var x = e.pagePos.x + e.bbox.x - BOX_PAD
            if (e.type === Element.MARKER && e.pagePos.x - e.offsetX <= m.pagePos.x + 4) continue
            var key = lineKey(m)
            if (!found[key]) found[key] = []
            found[key].push(x)
        }
        m = m.nextMeasure
    }
    return found
}

function shortfalls(all) {
    // per bar, the spaces its chords lack to keep the clear gap before the next chord or jump sign on the same line
    var short = []
    var signs = signsOf()
    for (var i = 0; i < all.length; i++) {
        var a = all[i], b = i + 1 < all.length ? all[i + 1] : null
        var key = lineKey(a.measure)
        var next = b && lineKey(b.measure) === key ? left(b) : 1e9
        var row = signs[key] || []
        for (var s = 0; s < row.length; s++) if (row[s] > left(a) && row[s] < next) next = row[s]
        if (next >= 1e9) continue
        var lack = right(a) + CLEAR + MARGIN - next
        if (lack > 0) {
            var at = -1
            for (var k = 0; k < short.length; k++) if (tickOf(short[k].measure) === tickOf(a.measure)) at = k
            if (at < 0) short.push({ measure: a.measure, lack: lack })
            else short[at].lack = Math.max(short[at].lack, lack)
        }
    }
    return short
}

function tickOf(measure) { return measure.firstSegment.tick }

function measures() {
    var found = []
    var m = curScore.firstMeasure
    while (m) { found.push(m); m = m.nextMeasure }
    return found
}

function hasNotes(measure) {
    var segment = measure.firstSegment
    while (segment) {
        if (segment.segmentType === Segment.ChordRest) {
            for (var track = 0; track < curScore.ntracks; track++) {
                var held = segment.elementAt(track)
                if (held && held.type === Element.CHORD) return true
            }
        }
        segment = segment.next
    }
    return false
}

var FIT_ROUNDS = 8

var MARGIN = 0.1  // spaces past the clear gap a fit aims for: the gate holds the gap with no tolerance

var TRIED = {}  // per bar tick, the last stretch set and the shortfall it left

function widen(short) {
    // each bar's next stretch from its own measured response of shortfall to stretch: chords inside a bar sit at shares of it, so a wider bar closes their gap by only a part of what it grew
    var perLine = {}  // bars per line: a bar alone on its line already spans it, so a wider stretch moves nothing and only runs away
    var all = measures()
    for (var j = 0; j < all.length; j++) perLine[lineKey(all[j])] = (perLine[lineKey(all[j])] || 0) + 1
    curScore.startCmd()
    for (var i = 0; i < short.length; i++) {
        var m = short[i].measure
        if (perLine[lineKey(m)] === 1) continue
        var w = m.bbox.width
        var s = m.userStretch
        var lack = short[i].lack
        var before = TRIED[tickOf(m)]
        var next = s * (w + lack) / w
        if (before && s - before.stretch > 1e-6 && before.lack - lack > 1e-3) {
            next = s + lack * (s - before.stretch) / (before.lack - lack)  // stretch that closes the rest at the rate just measured
        }
        TRIED[tickOf(m)] = { stretch: s, lack: lack }
        m.userStretch = Math.max(next, s * 1.01)
    }
    curScore.endCmd()
}

function isNoted(measure) {
    // whether a bar holds notes, by the caller's own score model when it gave 1
    return NOTED.length ? NOTED.indexOf(barIndex(measure)) >= 0 : hasNotes(measure)
}

function inner(measure) {
    // a bar's width after what opens its line, the width the checks compare
    return measure.bbox.width - (opening(measure) - measure.pagePos.x)
}

function even() {
    // a line's empty bars widened to its widest empty bar
    var lines = {}
    var all = measures()
    for (var i = 0; i < all.length; i++) {
        if (isNoted(all[i])) continue
        var key = lineKey(all[i])
        if (!lines[key]) lines[key] = []
        lines[key].push(all[i])
    }
    var moved = 0
    curScore.startCmd()
    for (var k in lines) {
        var widest = 0
        for (var j = 0; j < lines[k].length; j++) widest = Math.max(widest, inner(lines[k][j]))
        for (var n = 0; n < lines[k].length; n++) {
            var m = lines[k][n]
            if (widest - inner(m) > 0.1) { m.userStretch = m.userStretch * widest / inner(m); moved += 1 }
        }
    }
    curScore.endCmd()
    return moved
}

var NOTED = []  // 0-based bars holding notes, as the caller's own score model says

function barIndex(measure) {
    var at = 0
    var m = curScore.firstMeasure
    while (m && tickOf(m) !== tickOf(measure)) { at++; m = m.nextMeasure }
    return at
}

var OPENING_SIGN_SHIFT = 4  // spaces a sign opening a line moves right, clear of the line's bar number: 3 digits take about 3.4

function clearOpeningSigns() {
    // a segno or coda opening a line prints right of the line's bar number; the chord after it starts past it, since start() reads where it prints
    curScore.startCmd()
    var m = curScore.firstMeasure
    while (m) {
        var before = m.prevMeasure
        var opens = !before || lineKey(before) !== lineKey(m)
        var owned = m.elements
        for (var i = 0; owned && i < owned.length; i++) {
            var e = owned[i]
            if (e.type !== Element.MARKER || !e.bbox) continue
            var atStart = e.pagePos.x - e.offsetX <= m.pagePos.x + 4
            if (atStart) e.offsetX = opens ? OPENING_SIGN_SHIFT : 0
        }
        m = m.nextMeasure
    }
    curScore.endCmd()
}

function measureMode(all) {
    // on an unjustified render: per bar, the share its width must grow so every chord clears the next by the gap; chords sit at fixed shares of their bar, so 1 measurement gives it
    seatStarts(all)
    var bars = measures()
    var need = {}
    for (var i = 0; i < all.length; i++) {
        var a = all[i]
        var bar = a.measure
        var edge = bar.pagePos.x
        var width = bar.bbox.width
        var sameBar = i + 1 < all.length && tickOf(all[i + 1].measure) === tickOf(bar)
        var at, next
        if (isNoted(bar)) {  // notes set where a chord prints; the caller says which bars hold notes
            at = (left(a) - edge) / width
            next = sameBar ? (left(all[i + 1]) - edge) / width : 1
        } else {  // a bar of rests spaces its chords by beat
            var length = (bar.nextMeasure ? tickOf(bar.nextMeasure) : curScore.lastSegment.tick) - tickOf(bar)
            at = (a.segment.tick - tickOf(bar)) / length
            next = sameBar ? (all[i + 1].segment.tick - tickOf(bar)) / length : 1
        }
        var room = right(a) - left(a) + CLEAR + MARGIN
        var wanted = next > at ? room / (next - at) : width
        if (a.segment.tick === tickOf(bar) && next > 0) wanted = (left(a) - edge + room) / next  // a beat-1 chord sits a fixed distance in, past any sign: absolute room before the next chord's share
        need[tickOf(bar)] = Math.max(need[tickOf(bar)] || width, wanted)
    }
    curScore.setMetaTag("museSeatError", "")
    curScore.setMetaTag("museChordShare", bars.map(function (m) { return Math.max(1, (need[tickOf(m)] || m.bbox.width) / m.bbox.width).toFixed(4) }).join(","))
}

function main() {
    var given = curScore.metaTag("museInkBias")  // per chord, the spaces its ink starts past its text box, measured by the caller
    BIAS = given ? given.split(",").map(Number) : []
    CLEAR = Number(curScore.metaTag("museChordGap"))
    if (!(CLEAR > 0)) { api.log.error("museseat: no museChordGap tag"); curScore.setMetaTag("museSeatError", "no museChordGap tag"); return }
    var all = chords()
    var inked = curScore.metaTag("museInkWidth")
    INK = inked ? inked.split(",").map(Number) : []
    if (INK.length && INK.length !== all.length) { curScore.setMetaTag("museSeatError", "ink widths for " + INK.length + " chords, the score holds " + all.length); return }
    var noted = curScore.metaTag("museNoted")
    NOTED = noted ? noted.split(",").map(Number) : []
    clearOpeningSigns()
    if (curScore.metaTag("museMode") === "measure") { measureMode(all); return }
    var rounds = 0
    for (; rounds < FIT_ROUNDS; rounds++) {
        seatStarts(all)
        var short = shortfalls(all)
        var moved = curScore.metaTag("museEven") === "0" ? 0 : even()  // the caller evens from a fresh render when it says so
        var widening = curScore.metaTag("museEven") !== "0" || curScore.metaTag("museWiden") === "1"  // chord text sets no bar's width unless the caller allows it
        if ((!short.length || !widening) && !moved) break
        if (short.length && widening) widen(short)
    }
    seatStarts(all)
    var remaining = shortfalls(all)
    curScore.setMetaTag("museSeatError", remaining.length ? "chords still crowd in " + remaining.length + " bars" : "")
    lift(all)
    raiseText(all)
}

function raiseText(all) {
    // a line's cues, marks and tempi, in reading order, each started above every chord and every text already placed over the same span; automatic placement then stacks them and reserves their height
    var placed = {}
    for (var i = 0; i < all.length; i++) {
        var c = all[i].chord
        var key = lineKey(all[i].measure)
        if (!placed[key]) placed[key] = []
        placed[key].push({ x0: c.pagePos.x + c.bbox.x, x1: c.pagePos.x + c.bbox.x + c.bbox.width, top: c.pagePos.y + c.bbox.y })
    }
    var signBar = curScore.firstMeasure
    while (signBar) {  // a sign's box is an obstacle too: a mark or cue stacks above it, never over it
        var owned = signBar.elements
        for (var s = 0; owned && s < owned.length; s++) {
            var sign = owned[s]
            if ((sign.type !== Element.MARKER && sign.type !== Element.JUMP) || !sign.bbox) continue
            var signKey = lineKey(signBar)
            if (!placed[signKey]) placed[signKey] = []
            placed[signKey].push({ x0: sign.pagePos.x + sign.bbox.x - BOX_PAD, x1: sign.pagePos.x + sign.bbox.x + sign.bbox.width + BOX_PAD, top: sign.pagePos.y + sign.bbox.y - BOX_PAD })
        }
        signBar = signBar.nextMeasure
    }
    var texts = []
    var segment = curScore.firstSegment()
    while (segment) {
        var notes = segment.annotations
        for (var n = 0; n < notes.length; n++) {
            var t = notes[n]
            if (t.type === Element.HARMONY || t.type === Element.FERMATA || !t.bbox || t.track !== 0) continue
            if (t.pagePos.y + t.bbox.y >= segment.parent.pagePos.y) continue  // below the staff: no chord row there, and MuseScore stacks those itself
            texts.push({ text: t, key: lineKey(segment.parent), x0: t.pagePos.x + t.bbox.x, x1: t.pagePos.x + t.bbox.x + t.bbox.width, top: t.pagePos.y + t.bbox.y, height: t.bbox.height })
        }
        segment = segment.next
    }
    texts.sort(function (a, b) { return a.key === b.key ? a.x0 - b.x0 : (a.key < b.key ? -1 : 1) })
    var ends = {}  // per line, the right end of its last bar: no text may run past it
    var bar = curScore.firstMeasure
    while (bar) {
        var k2 = lineKey(bar)
        ends[k2] = Math.max(ends[k2] || -1e9, bar.pagePos.x + bar.bbox.width)
        bar = bar.nextMeasure
    }
    curScore.startCmd()
    for (var k = 0; k < texts.length; k++) {
        var item = texts[k]
        var row = placed[item.key] || []
        var ceiling = 1e9
        for (var r = 0; r < row.length; r++) if (row[r].x0 - TEXT_SLACK < item.x1 && item.x0 < row[r].x1 + TEXT_SLACK) ceiling = Math.min(ceiling, row[r].top)
        if (ends[item.key] !== undefined && item.x1 > ends[item.key] - TEXT_SLACK) {  // a cue on a line's last bar runs past the margin: it ends inside the line's end instead
            var back = item.x1 - (ends[item.key] - TEXT_SLACK)
            item.text.offsetX = item.text.offsetX - back
            item.x0 -= back
            item.x1 -= back
        }
        var top = item.top
        if (ceiling < 1e9 && item.top + item.height > ceiling - GAP - TEXT_SLACK) {
            var rise = item.top + item.height - (ceiling - GAP - TEXT_SLACK)
            item.text.offsetY = item.text.offsetY - rise  // placement stays automatic: MuseScore stacks texts and makes room above the line
            top = item.top - rise
        }
        if (!placed[item.key]) placed[item.key] = []
        placed[item.key].push({ x0: item.x0, x1: item.x1, top: top })
    }
    curScore.endCmd()
}
