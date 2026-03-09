# cs2_trimmer  
Upload .mp4 cs2 clips, and it will automatically trim the clip to only include kill instances.     
- Custom specify the buffer time before or after each kill instance.  
- Or enable full span mode to only trim before first kill and after last kill.
------------------------------------------------------------------------------------------
<img src="https://raw.githubusercontent.com/toobad000/cs2_trimmer/main/img/cs2trimmer.png" alt="cs2_trimmer_image">

------------------------------------------------------------------------------------------  
Current Status:  
I haven't had time to fix all the bugs or host it as a website, some clips may not trim correctly. The program scans the top right corner for red borders indicating kills/assists, that being said it will also include assists in the clip and may identify some false positives. In addition the stretch to fill feature is not yet working for any clips that may originally show black bars 
