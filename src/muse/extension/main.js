function chords() {
    var found = []
    var segment = curScore.firstSegment()
    while (segment) {
        var notes = segment.annotations
        for (var i = 0; i < notes.length; i++) {
            if (notes[i].type === Element.HARMONY && notes[i].track === 0) {
                found.push({ chord: notes[i], segment: segment, measure: segment.parent })
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
    // the highest top of anything printed over the staff in a segment, chord symbols aside
    var top = 1e9
    var notes = segment.annotations
    for (var i = 0; i < notes.length; i++) {
        if (notes[i].type !== Element.HARMONY && notes[i].bbox) {
            var y = notes[i].pagePos.y + notes[i].bbox.y
            top = Math.min(top, y)
        }
    }
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

var GAP = 0.5

var BIAS = []

function main() {
    var given = curScore.metaTag("museInkBias")  // per chord, the spaces its ink starts past its text box, measured by the caller
    BIAS = given ? given.split(",").map(Number) : []
    var all = chords()
    api.log.info("museseat chords " + all.length)
    for (var round = 0; round < 3; round++) {
        var off = 0
        var worst = 0
        curScore.startCmd()
        for (var i = 0; i < all.length; i++) {
            var item = all[i]
            if (item.segment.tick !== item.measure.firstSegment.tick) continue
            var left = item.chord.pagePos.x + item.chord.bbox.x
            var gap = left - start(item.measure) + (BIAS[i] || 0)
            if (Math.abs(gap) > 0.05) {
                item.chord.autoplace = false
                item.chord.offsetX = item.chord.offsetX - gap
                off += 1
                worst = Math.max(worst, Math.abs(gap))
            }
        }
        curScore.endCmd()
        api.log.info("museseat round " + round + " moved " + off + " worst " + worst)
    }
    lift(all)
    api.log.info("museseat lifted")
}
