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
    // the bar's left edge, or after the clef, key and time signature that open a line, and after any sign printed there
    var edge = measure.pagePos.x
    var before = measure.prevMeasure
    if (before && Math.abs(before.pagePos.y - measure.pagePos.y) < 0.5) {  // the barline closing the bar before, on the same line
        var last = before.lastSegment
        while (last && last.tick >= measure.firstSegment.tick - 1 && last.segmentType !== Segment.EndBarLine) last = last.prev
        var line = last ? last.elementAt(0) : null
        if (line && line.bbox) edge = line.pagePos.x + line.bbox.x + line.bbox.width
    }
    var owned = measure.elements
    for (var m = 0; owned && m < owned.length; m++) {
        if (owned[m].type === Element.MARKER && owned[m].bbox && owned[m].pagePos.x <= measure.pagePos.x + 4) {
            edge = Math.max(edge, owned[m].pagePos.x + owned[m].bbox.x + owned[m].bbox.width + 0.5)
        }
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
    for (var track = 0; track < 4; track++) {
        var held = segment.elementAt(track)
        if (held && held.bbox) top = Math.min(top, held.pagePos.y + held.bbox.y)
        if (held && held.tuplet && held.tuplet.bbox) top = Math.min(top, held.tuplet.pagePos.y + held.tuplet.bbox.y)
    }
    return top
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
        clear[key2] = Math.min(clear[key2] === undefined ? 1e9 : clear[key2], above(segment))
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
            var bottomOf = top + part.bbox.height
            var page = part.parent.parent.pagenumber
            var best = null
            for (var u = 0; u < lineTops.length; u++) {
                var bits = lineTops[u].split(":")
                if (Number(bits[0]) !== page || Number(bits[1]) < bottomOf - 0.5) continue
                if (best === null || Number(bits[1]) < Number(best.split(":")[1])) best = lineTops[u]
            }
            if (best !== null && Number(best.split(":")[1]) - bottomOf < 8) clear[best] = Math.min(clear[best] === undefined ? 1e9 : clear[best], top)
        }
    }
    for (var i = 0; i < all.length; i++) {
        var key = lineKey(all[i].measure)
        var bottom = all[i].chord.pagePos.y + all[i].chord.bbox.y + all[i].chord.bbox.height
        var need = clear[key] - GAP - bottom
        lines[key] = Math.min(lines[key] === undefined ? 0 : lines[key], need)
    }
    curScore.startCmd()
    for (var j = 0; j < all.length; j++) {
        var line = lineKey(all[j].measure)
        all[j].chord.autoplace = false
        if (lines[line] < 0) all[j].chord.offsetY = all[j].chord.offsetY + lines[line]
    }
    curScore.endCmd()
}

var GAP = 0.5  // spaces between a line's chords and the highest thing below them

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

function shortfalls(all) {
    // per bar, the spaces its chords lack to keep the clear gap before the next chord on the same line
    var short = []
    for (var i = 0; i + 1 < all.length; i++) {
        var a = all[i], b = all[i + 1]
        if (lineKey(a.measure) !== lineKey(b.measure)) continue
        var lack = right(a) + CLEAR + MARGIN - left(b)
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
    curScore.startCmd()
    for (var i = 0; i < short.length; i++) {
        var m = short[i].measure
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

function even() {
    // a line's empty bars widened to its widest empty bar
    var lines = {}
    var all = measures()
    for (var i = 0; i < all.length; i++) {
        if (hasNotes(all[i])) continue
        var key = lineKey(all[i])
        if (!lines[key]) lines[key] = []
        lines[key].push(all[i])
    }
    var moved = 0
    curScore.startCmd()
    for (var k in lines) {
        var widest = 0
        for (var j = 0; j < lines[k].length; j++) widest = Math.max(widest, lines[k][j].bbox.width)
        for (var n = 0; n < lines[k].length; n++) {
            var m = lines[k][n]
            if (widest - m.bbox.width > 0.1) { m.userStretch = m.userStretch * widest / m.bbox.width; moved += 1 }
        }
    }
    curScore.endCmd()
    return moved
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
        var at = (left(a) - edge) / width
        var next = i + 1 < all.length && tickOf(all[i + 1].measure) === tickOf(bar) ? (left(all[i + 1]) - edge) / width : 1
        var room = right(a) - left(a) + CLEAR + MARGIN
        var wanted = next > at ? room / (next - at) : width
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
    if (curScore.metaTag("museMode") === "measure") { measureMode(all); return }
    var rounds = 0
    for (; rounds < FIT_ROUNDS; rounds++) {
        seatStarts(all)
        var short = shortfalls(all)
        var moved = even()
        if (!short.length && !moved) break
        if (short.length) widen(short)
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
    var texts = []
    var segment = curScore.firstSegment()
    while (segment) {
        var notes = segment.annotations
        for (var n = 0; n < notes.length; n++) {
            var t = notes[n]
            if (t.type === Element.HARMONY || !t.bbox || t.track !== 0) continue
            texts.push({ text: t, key: lineKey(segment.parent), x0: t.pagePos.x + t.bbox.x, x1: t.pagePos.x + t.bbox.x + t.bbox.width, top: t.pagePos.y + t.bbox.y, height: t.bbox.height })
        }
        segment = segment.next
    }
    texts.sort(function (a, b) { return a.key === b.key ? a.x0 - b.x0 : (a.key < b.key ? -1 : 1) })
    curScore.startCmd()
    for (var k = 0; k < texts.length; k++) {
        var item = texts[k]
        var row = placed[item.key] || []
        var ceiling = 1e9
        for (var r = 0; r < row.length; r++) if (row[r].x0 < item.x1 && item.x0 < row[r].x1) ceiling = Math.min(ceiling, row[r].top)
        var top = item.top
        if (ceiling < 1e9 && item.top + item.height > ceiling - GAP) {
            var rise = item.top + item.height - (ceiling - GAP)
            item.text.offsetY = item.text.offsetY - rise  // placement stays automatic: MuseScore stacks texts and makes room above the line
            top = item.top - rise
        }
        if (!placed[item.key]) placed[item.key] = []
        placed[item.key].push({ x0: item.x0, x1: item.x1, top: top })
    }
    curScore.endCmd()
}
