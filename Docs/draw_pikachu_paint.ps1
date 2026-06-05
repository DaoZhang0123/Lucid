Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class SendMouse3 {
  [StructLayout(LayoutKind.Sequential)] public struct INPUT { public int type; public MOUSEINPUT mi; }
  [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
  [DllImport("user32.dll", SetLastError=true)] public static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
"@
$INPUT_MOUSE=0
$MOVE=0x0001; $LEFTDOWN=0x0002; $LEFTUP=0x0004; $ABS=0x8000; $VDESK=0x4000
$sw=[System.Windows.Forms.SystemInformation]::VirtualScreen.Width
$sh=[System.Windows.Forms.SystemInformation]::VirtualScreen.Height
$sx=[System.Windows.Forms.SystemInformation]::VirtualScreen.Left
$sy=[System.Windows.Forms.SystemInformation]::VirtualScreen.Top
function SendMouse([int]$x,[int]$y,[uint32]$flags){
  $nx=[int](($x-$sx)*65535/($sw-1)); $ny=[int](($y-$sy)*65535/($sh-1))
  $inp = New-Object SendMouse3+INPUT
  $inp.type = $INPUT_MOUSE
  $inp.mi.dx = $nx; $inp.mi.dy = $ny; $inp.mi.mouseData = 0; $inp.mi.dwFlags = ($flags -bor $ABS -bor $VDESK); $inp.mi.time = 0; $inp.mi.dwExtraInfo = [IntPtr]::Zero
  [SendMouse3+INPUT[]]$arr=@($inp)
  [SendMouse3]::SendInput(1,$arr,[Runtime.InteropServices.Marshal]::SizeOf([type][SendMouse3+INPUT])) | Out-Null
}
$paint = Get-Process | Where-Object { $_.MainWindowTitle -like '*画图*' -or $_.MainWindowTitle -like '*Paint*' } | Select-Object -First 1
if ($paint) { [SendMouse3]::SetForegroundWindow($paint.MainWindowHandle) | Out-Null }
Start-Sleep -Milliseconds 500
[System.Windows.Forms.SendKeys]::SendWait('^a'); Start-Sleep -Milliseconds 150
[System.Windows.Forms.SendKeys]::SendWait('{DEL}'); Start-Sleep -Milliseconds 250
[System.Windows.Forms.SendKeys]::SendWait('p'); Start-Sleep -Milliseconds 250
# image-local to screen offset measured by Lucid earlier. Paint is same window; coordinates are kept well inside canvas.
$offX=69; $offY=215
function Spt([int]$x,[int]$y){ [pscustomobject]@{X=($x+$offX);Y=($y+$offY)} }
function Stroke($pts,[int]$delay=2){
  if($pts.Count -lt 2){return}
  SendMouse $pts[0].X $pts[0].Y $MOVE; Start-Sleep -Milliseconds 60
  SendMouse $pts[0].X $pts[0].Y $LEFTDOWN; Start-Sleep -Milliseconds 70
  foreach($p in $pts){ SendMouse $p.X $p.Y $MOVE; Start-Sleep -Milliseconds $delay }
  SendMouse $pts[-1].X $pts[-1].Y $LEFTUP; Start-Sleep -Milliseconds 130
}
function Path($pairs){ $r=@(); foreach($p in $pairs){ $r += Spt $p[0] $p[1] }; return $r }
function EllipsePath([int]$cx,[int]$cy,[int]$rx,[int]$ry,[int]$n=240){ $r=@(); for($i=0;$i -le $n;$i++){ $t=2*[Math]::PI*$i/$n; $r += Spt ([int]($cx+$rx*[Math]::Cos($t))) ([int]($cy+$ry*[Math]::Sin($t))) }; return $r }
function ArcPath([int]$cx,[int]$cy,[int]$rx,[int]$ry,[double]$a1,[double]$a2,[int]$n=120){ $r=@(); for($i=0;$i -le $n;$i++){ $t=($a1+($a2-$a1)*$i/$n)*[Math]::PI/180; $r += Spt ([int]($cx+$rx*[Math]::Cos($t))) ([int]($cy+$ry*[Math]::Sin($t))) }; return $r }
# Draw a deliberately large line-art Pikachu stroke by stroke, centered in the visible canvas.
Stroke (EllipsePath 815 515 170 140 260) 1
Stroke (Path @((690,425),(620,245),(765,388),(690,425))) 2
Stroke (Path @((935,400),(1045,245),(1005,450),(935,400))) 2
Stroke (Path @((645,292),(700,352))) 2
Stroke (Path @((1018,305),(970,362))) 2
Stroke (EllipsePath 755 500 25 35 100) 1
Stroke (EllipsePath 885 500 25 35 100) 1
Stroke (EllipsePath 746 488 8 10 40) 1
Stroke (EllipsePath 876 488 8 10 40) 1
Stroke (EllipsePath 700 565 40 30 100) 1
Stroke (EllipsePath 940 565 40 30 100) 1
Stroke (EllipsePath 820 538 9 6 50) 1
Stroke (ArcPath 790 550 38 34 18 170 80) 2
Stroke (ArcPath 850 550 38 34 10 162 80) 2
Stroke (ArcPath 820 585 52 36 25 155 90) 2
Stroke (ArcPath 815 710 150 130 200 520 180) 1
Stroke (Path @((665,650),(600,690),(665,715))) 2
Stroke (Path @((965,650),(1030,690),(970,715))) 2
Stroke (ArcPath 730 835 60 26 185 350 80) 2
Stroke (ArcPath 905 835 60 26 190 355 80) 2
Stroke (ArcPath 815 705 90 56 28 152 90) 2
Stroke (Path @((980,650),(1095,585),(1065,650),(1195,620),(1088,730),(1118,665),(1000,735))) 2
Stroke (Path @((728,620),(700,660),(740,645))) 2
Stroke (Path @((902,620),(935,660),(895,645))) 2
Stroke (Path @((640,525),(595,510))) 2
Stroke (Path @((990,525),(1038,510))) 2
SendMouse (815+$offX) (515+$offY) $MOVE
