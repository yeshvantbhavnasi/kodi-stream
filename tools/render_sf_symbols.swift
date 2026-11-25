#!/usr/bin/env swift
import AppKit
import Foundation

// Renders authentic Apple SF Symbols as crisp white transparent PNGs for Stream UI & Player OSD.
// Usage: swift render_sf_symbols.swift <stream_media_dir> <skin_osd_dir>

guard CommandLine.arguments.count >= 3 else {
    fputs("Usage: render_sf_symbols.swift <stream_media_dir> <skin_osd_dir>\n", stderr)
    exit(1)
}

let streamMediaDir = CommandLine.arguments[1]
let skinOsdDir = CommandLine.arguments[2]

func renderSymbol(_ symbolName: String, to path: String, size: CGFloat = 128, weight: NSFont.Weight = .regular, pointRatio: CGFloat = 0.62) {
    let config = NSImage.SymbolConfiguration(pointSize: size * pointRatio, weight: weight, scale: .large)
        .applying(.init(paletteColors: [.white]))
    guard let sym = NSImage(systemSymbolName: symbolName, accessibilityDescription: nil)?.withSymbolConfiguration(config) else {
        fputs("Warning: SF Symbol not found: \(symbolName)\n", stderr)
        return
    }

    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: Int(size),
        pixelsHigh: Int(size),
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    )!
    rep.size = NSSize(width: size, height: size)

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    NSColor.clear.set()
    NSRect(x: 0, y: 0, width: size, height: size).fill()

    // Center symbol optically inside the square canvas preserving aspect ratio
    let s = sym.size
    let maxDim = size * 0.78
    let scale = min(maxDim / max(s.width, 1), maxDim / max(s.height, 1))
    let w = s.width * scale
    let h = s.height * scale
    let rect = NSRect(x: (size - w) / 2.0, y: (size - h) / 2.0, width: w, height: h)
    sym.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1.0)
    NSGraphicsContext.restoreGraphicsState()

    if let png = rep.representation(using: .png, properties: [:]) {
        try? png.write(to: URL(fileURLWithPath: path))
    }
}

let topBarIcons: [(String, String, NSFont.Weight)] = [
    ("search-thin.png", "magnifyingglass", .medium),
    ("mic-thin.png", "mic.fill", .regular),
    ("person-thin.png", "person.crop.circle", .regular),
    ("gear-thin.png", "gearshape", .regular),
    ("close-thin.png", "xmark", .medium),
    ("backspace-thin.png", "delete.left", .regular),
    ("tab-home.png", "house.fill", .medium),
    ("tab-movies.png", "film", .medium),
    ("tab-shows.png", "tv", .medium),
    ("tab-live.png", "dot.radiowaves.left.and.right", .medium),
    ("tab-sports.png", "sportscourt", .medium),
    ("tab-mylist.png", "bookmark.fill", .medium),
    ("tab-kids.png", "sparkles", .medium),
    ("tab-downloads.png", "arrow.down.circle.fill", .medium),
]

let osdIcons: [(String, String, NSFont.Weight)] = [
    ("play.png", "play.fill", .regular),
    ("pause.png", "pause.fill", .regular),
    ("stop.png", "stop.fill", .regular),
    ("rw10.png", "gobackward.10", .medium),
    ("ff10.png", "goforward.10", .medium),
    ("subtitles.png", "captions.bubble", .medium),
    ("audio.png", "speaker.wave.2.fill", .regular),
    ("settings.png", "slider.horizontal.3", .medium),
    ("back.png", "chevron.backward", .semibold),
]

let kodiOsdButtons: [(String, String, NSFont.Weight)] = [
    ("play.png", "play.fill", .regular),
    ("pause.png", "pause.fill", .regular),
    ("stop.png", "stop.fill", .regular),
    ("rewind.png", "backward.fill", .regular),
    ("forward.png", "forward.fill", .regular),
    ("previous.png", "backward.end.fill", .regular),
    ("next.png", "forward.end.fill", .regular),
    ("settings-subtitle.png", "captions.bubble.fill", .regular),
    ("settings.png", "slider.horizontal.3", .medium),
    ("bookmarks.png", "bookmark.fill", .regular),
    ("information.png", "info.circle", .medium),
    ("playlist.png", "list.bullet", .medium),
    ("channels.png", "tv", .medium),
    ("guide.png", "calendar", .medium),
    ("home.png", "house.fill", .regular),
    ("record.png", "record.circle", .medium),
    ("record-white.png", "record.circle.fill", .medium),
    ("teletext.png", "text.alignleft", .medium),
    ("stereoscopic.png", "view.3d", .medium),
]

for (file, sym, weight) in topBarIcons {
    renderSymbol(sym, to: (streamMediaDir as NSString).appendingPathComponent(file), size: 128, weight: weight)
}
for (file, sym, weight) in osdIcons {
    renderSymbol(sym, to: (skinOsdDir as NSString).appendingPathComponent(file), size: 128, weight: weight)
}
let btnDir = (skinOsdDir as NSString).appendingPathComponent("buttons")
try? FileManager.default.createDirectory(atPath: btnDir, withIntermediateDirectories: true)
for (file, sym, weight) in kodiOsdButtons {
    renderSymbol(sym, to: (btnDir as NSString).appendingPathComponent(file), size: 128, weight: weight, pointRatio: 0.44)
}
print("Rendered \(topBarIcons.count + osdIcons.count + kodiOsdButtons.count) Apple SF Symbols.")
