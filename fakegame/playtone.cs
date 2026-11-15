// playtone.cs — 假游戏桩
//
// 为什么需要它：引擎按「进程名」匹配游戏，匹配到了才愿意进入"游戏进行中"的震动模式。
// 手上没游戏、或游戏没在跑，就没法验证链路。
// 做法：编译出一个和游戏同名的可执行文件，它只做两件事——循环放一段音、然后长睡。
// 引擎看到进程名，音频回环里也有声音，于是一切照常。
//
// 编译（见 build.bat）：
//   build.bat ForzaHorizon5.exe
// 运行：
//   ForzaHorizon5.exe [wav路径] [秒数]

using System;
using System.Media;

class PlayTone
{
    static int Main(string[] args)
    {
        string wav = args.Length > 0 ? args[0] : @"C:\Windows\Media\Alarm01.wav";
        double secs = 3600;
        if (args.Length > 1)
        {
            double.TryParse(args[1], out secs);
            if (secs <= 0) secs = 3600;
        }

        try
        {
            SoundPlayer sp = new SoundPlayer(wav);
            sp.PlayLooping();
        }
        catch (Exception e)
        {
            Console.Error.WriteLine("放音失败: " + e.Message);
            return 2;
        }

        Console.Out.WriteLine("READY");
        System.Threading.Thread.Sleep((int)(secs * 1000));
        return 0;
    }
}
