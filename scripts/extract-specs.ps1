# Extracts plain text from every .docx in docs/specs into a sibling .txt file,
# so the specs can be searched with ordinary tools (build prompt section 1).
# Paragraphs become lines; table cells become lines; tabs are kept.
param(
    [string]$SpecDir = (Join-Path $PSScriptRoot '..\docs\specs')
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem

Get-ChildItem -LiteralPath $SpecDir -Filter *.docx | ForEach-Object {
    $zip = [System.IO.Compression.ZipFile]::OpenRead($_.FullName)
    try {
        $entry = $zip.GetEntry('word/document.xml')
        $reader = New-Object System.IO.StreamReader($entry.Open())
        $xml = $reader.ReadToEnd()
        $reader.Close()
    } finally {
        $zip.Dispose()
    }
    $text = $xml -replace '</w:p>', "`n" -replace '<w:tab/>', "`t" -replace '<w:br/>', "`n" -replace '<[^>]+>', ''
    $text = [System.Net.WebUtility]::HtmlDecode($text)
    $out = [System.IO.Path]::ChangeExtension($_.FullName, '.txt')
    # UTF-8 without BOM, LF line endings, to match .gitattributes.
    [System.IO.File]::WriteAllText($out, ($text -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding($false)))
    '{0} -> {1} ({2:N0} chars)' -f $_.Name, (Split-Path $out -Leaf), $text.Length
}
