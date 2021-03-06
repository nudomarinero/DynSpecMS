from pyrap.tables import table
import sys
from DDFacet.Other import MyLogger
log=MyLogger.getLogger("DynSpecMS")
from DDFacet.Array import shared_dict
from DDFacet.Other.AsyncProcessPool import APP, WorkerProcessError
from DDFacet.Other import Multiprocessing
from DDFacet.Other import ModColor
from DDFacet.Other.progressbar import ProgressBar
import numpy as np
from astropy.time import Time
from astropy import constants as const
import os
from killMS.Other import reformat
from DDFacet.Other import AsyncProcessPool
from dynspecms_version import version

def AngDist(ra0,ra1,dec0,dec1):
    AC=np.arccos
    C=np.cos
    S=np.sin
    return AC(S(dec0)*S(dec1)+C(dec0)*C(dec1)*C(ra0-ra1))

class ClassDynSpecMS():
    def __init__(self, ListMSName, ColName="DATA", ModelName="PREDICT_KMS", UVRange=[1.,1000.], 
                 SolsName=None,
                 FileCoords=None,
                 Radius=3.,
                 NOff=-1,
                 Image=None,
                 SolsDir=None,
                 NCPU=1):
        self.ListMSName = sorted(ListMSName)#[0:2]
        self.nMS         = len(self.ListMSName)
        self.ColName    = ColName
        self.ModelName  = ModelName
        self.OutName    = self.ListMSName[0].split("/")[-1].split("_")[0]
        self.UVRange    = UVRange
        self.ReadMSInfos()
        self.Radius=Radius
        self.Image = Image
        self.SolsDir=SolsDir
        #self.PosArray=np.genfromtxt(FileCoords,dtype=[('Name','S200'),("ra",np.float64),("dec",np.float64),('Type','S200')],delimiter="\t")

        # identify version in logs
        print>>log,"DynSpecMS version %s starting up" % version()
        
        # should we use the surveys DB?
        if 'DDF_PIPELINE_DATABASE' in os.environ:
            print>>log,"Using the surveys database"
            from surveys_db import SurveysDB
            with SurveysDB() as sdb:
                sdb.cur.execute('select * from transients')
                result=sdb.cur.fetchall()
            # convert to a list, then to ndarray, then to recarray
            l=[]
            for r in result:
                l.append((r['id'],r['ra'],r['decl'],r['type']))
            self.PosArray=np.asarray(l,dtype=[('Name','S200'),("ra",np.float64),("dec",np.float64),('Type','S200')])
            print>>log,"Created an array with %i records" % len(result)
        else:
            if FileCoords is None:
                FileCoords="Transient_LOTTS.csv"
                if not os.path.isfile(FileCoords):
                    ssExec="wget -q --user=anonymous ftp://ftp.strw.leidenuniv.nl/pub/tasse/%s -O %s"%(FileCoords,FileCoords)
                    print>>log,"Downloading %s"%FileCoords
                    print>>log, "   Executing: %s"%ssExec
                    os.system(ssExec)
            self.PosArray=np.genfromtxt(FileCoords,dtype=[('Name','S200'),("ra",np.float64),("dec",np.float64),('Type','S200')],delimiter=",")[()]
        self.PosArray=self.PosArray.view(np.recarray)
        self.PosArray.ra*=np.pi/180.
        self.PosArray.dec*=np.pi/180.

        
        NOrig=self.PosArray.shape[0]
        Dist=AngDist(self.ra0,self.PosArray.ra,self.dec0,self.PosArray.dec)
        ind=np.where(Dist<Radius*np.pi/180)[0]
        self.PosArray=self.PosArray[ind]
        self.NDirSelected=self.PosArray.shape[0]

        print>>log,"Selected %i target [out of the %i in the original list]"%(self.NDirSelected,NOrig)
        if self.NDirSelected==0:
            print>>log,ModColor.Str("   Have found no sources - returning")
            self.killWorkers()
            return 
        
        if NOff==-1:
            NOff=self.PosArray.shape[0]*2
        if NOff is not None:
            print>>log,"Including %i off targets"%(NOff)
            self.PosArray=np.concatenate([self.PosArray,self.GiveOffPosArray(NOff)])
            self.PosArray=self.PosArray.view(np.recarray)
        self.NDir=self.PosArray.shape[0]
        print>>log,"For a total of %i targets"%(self.NDir)


        self.DicoDATA = shared_dict.create("DATA")
        self.DicoGrids = shared_dict.create("Grids")
        self.DicoGrids["GridLinPol"] = np.zeros((self.NDir,self.NChan, self.NTimes, 4), np.complex128)
        self.DicoGrids["GridWeight"] = np.zeros((self.NDir,self.NChan, self.NTimes, 4), np.complex128)

        self.SolsName   = SolsName
        self.DoJonesCorr=False
        if self.SolsName:
            self.DoJonesCorr=True
            self.DicoJones=shared_dict.create("DicoJones")


        APP.registerJobHandlers(self)
        AsyncProcessPool.init(ncpu=NCPU,affinity=0)
        APP.startWorkers()


    def GiveOffPosArray(self,NOff):
        print>>log,"Making random off catalog with %i directions"%NOff
        CatOff=np.zeros((NOff,),self.PosArray.dtype)
        CatOff=CatOff.view(np.recarray)
        CatOff.Type="Off"
        NDone=0
        while NDone<NOff:
            dx=np.random.rand(1)[0]*self.Radius*np.pi/180
            dy=np.random.rand(1)[0]*self.Radius*np.pi/180
            ra=self.ra0+dx
            dec=self.dec0+dy
            d=AngDist(self.ra0,ra,self.dec0,dec)
            if d<self.Radius*np.pi/180:
                CatOff.ra[NDone]=ra
                CatOff.dec[NDone]=dec
                CatOff.Name[NDone]="Off%4.4i"%NDone
                NDone+=1
        return CatOff

    def ReadMSInfos(self):
        DicoMSInfos = {}

        MSName=self.ListMSName[0]
        t0  = table(MSName, ack=False)
        tf0 = table("%s::SPECTRAL_WINDOW"%self.ListMSName[0], ack=False)
        self.ChanWidth = tf0.getcol("CHAN_WIDTH").ravel()[0]
        tf0.close()
        self.times = np.unique(t0.getcol("TIME"))
        t0.close()

        tField = table("%s::FIELD"%MSName, ack=False)
        self.ra0, self.dec0 = tField.getcol("PHASE_DIR").ravel() # radians!
        tField.close()

        pBAR = ProgressBar(Title="Reading metadata")
        pBAR.render(0, self.nMS)
   
        #for iMS, MSName in enumerate(sorted(self.ListMSName)):
        for iMS, MSName in enumerate(self.ListMSName):
            try:
                t = table(MSName, ack=False)
            except Exception as e:
                s = str(e)
                DicoMSInfos[iMS] = {"Readable": False,
                                    "Exception": s}
                pBAR.render(iMS+1, self.nMS)
                continue

            if self.ColName not in t.colnames():
                DicoMSInfos[iMS] = {"Readable": False,
                                    "Exception": "Missing colname %s"%self.ColName}
                pBAR.render(iMS+1, self.nMS)
                continue

            
            if  self.ModelName and (self.ModelName not in t.colnames()):
                DicoMSInfos[iMS] = {"Readable": False,
                                    "Exception": "Missing colname %s"%self.ModelName}
                pBAR.render(iMS+1, self.nMS)
                continue
            

            tf = table("%s::SPECTRAL_WINDOW"%MSName, ack=False)
            ThisTimes = np.unique(t.getcol("TIME"))
            if not np.allclose(ThisTimes, self.times):
                raise ValueError("should have the same times")
            DicoMSInfos[iMS] = {"MSName": MSName,
                            "ChanFreq":   tf.getcol("CHAN_FREQ").ravel(),  # Hz
                            "ChanWidth":  tf.getcol("CHAN_WIDTH").ravel(), # Hz
                            "times":      ThisTimes,
                            "startTime":  Time(ThisTimes[0]/(24*3600.), format='mjd', scale='utc').isot,
                            "stopTime":   Time(ThisTimes[-1]/(24*3600.), format='mjd', scale='utc').isot,
                            "deltaTime":  (ThisTimes[-1] - ThisTimes[0])/3600., # h
                            "Readable":   True}
            if DicoMSInfos[iMS]["ChanWidth"][0] != self.ChanWidth:
                raise ValueError("should have the same chan width")
            pBAR.render(iMS+1, self.nMS)
            
        for iMS in range(self.nMS):
            if not DicoMSInfos[iMS]["Readable"]:
                print>>log, ModColor.Str("Problem reading %s"%MSName)
                print>>log, ModColor.Str("   %s"%DicoMSInfos[iMS]["Exception"])
                

        t.close()
        tf.close()
        self.DicoMSInfos = DicoMSInfos
        self.FreqsAll    = np.array([DicoMSInfos[iMS]["ChanFreq"] for iMS in DicoMSInfos.keys() if DicoMSInfos[iMS]["Readable"]])
        self.Freq_minmax = np.min(self.FreqsAll), np.max(self.FreqsAll)
        self.NTimes      = self.times.size
        f0, f1           = self.Freq_minmax
        self.NChan       = int((f1 - f0)/self.ChanWidth) + 1

        # Fill properties
        self.tStart = DicoMSInfos[0]["startTime"]
        self.tStop  = DicoMSInfos[0]["stopTime"] 
        self.fMin   = self.Freq_minmax[0]
        self.fMax   = self.Freq_minmax[1]

        self.iCurrentMS=0


    def LoadNextMS(self):
        iMS=self.iCurrentMS
        if not self.DicoMSInfos[iMS]["Readable"]: 
            print>>log, "Skipping [%i/%i]: %s"%(iMS+1, self.nMS, self.ListMSName[iMS])
            self.iCurrentMS+=1
            return "NotRead"
        print>>log, "Reading [%i/%i]: %s"%(iMS+1, self.nMS, self.ListMSName[iMS])

        MSName=self.ListMSName[self.iCurrentMS]
        
        t      = table(MSName, ack=False)
        data   = t.getcol(self.ColName)

        if self.ModelName:
            print>>log,"  Substracting %s from %s"%(self.ModelName,self.ColName)
            data-=t.getcol(self.ModelName)

        flag   = t.getcol("FLAG")
        times  = t.getcol("TIME")
        A0, A1 = t.getcol("ANTENNA1"), t.getcol("ANTENNA2")
        u, v, w = t.getcol("UVW").T
        t.close()
        d = np.sqrt(u**2 + v**2 + w**2)
        uv0, uv1         = np.array(self.UVRange) * 1000
        indUV = np.where( (d<uv0)|(d>uv1) )[0]
        flag[indUV, :, :] = 1 # flag according to UV selection
        data[flag] = 0 # put down to zeros flagged visibilities

        f0, f1           = self.Freq_minmax
        nch  = self.DicoMSInfos[iMS]["ChanFreq"].size

        # Considering another position than the phase center
        u0 = u.reshape( (-1, 1, 1) )
        v0 = v.reshape( (-1, 1, 1) )
        w0 = w.reshape( (-1, 1, 1) )
        self.DicoDATA["iMS"]=self.iCurrentMS
        self.DicoDATA["data"]=data
        self.DicoDATA["flag"]=flag
        self.DicoDATA["times"]=times
        self.DicoDATA["A0"]=A0
        self.DicoDATA["A1"]=A1
        self.DicoDATA["u"]=u0
        self.DicoDATA["v"]=v0
        self.DicoDATA["w"]=w0
        
        if self.DoJonesCorr:
            # Extract Jones matrices that will be appliedto the visibilities
            FileName="%s/%s"%(MSName, self.SolsName)

            if not(self.SolsDir):
                FileName="%s/%s"%(MSName, self.SolsName)
            else:
                _MSName=reformat.reformat(MSName).split("/")[-2]
                DirName=os.path.abspath("%s%s"%(reformat.reformat(self.SolsDir),_MSName))
                if not os.path.isdir(DirName):
                    os.makedirs(DirName)
                FileName="%s/killMS.%s.sols.npz"%(DirName,self.SolsName)


            print>>log,"Reading Jones matrices solution file:"
            print>>log,"   %s"%FileName
            JonesSols = np.load(FileName)
            self.DicoJones["G"]=self.NormJones(JonesSols["Sols"]["G"]) # Normalize Jones matrices
            self.DicoJones['tm']=(JonesSols["Sols"]["t0"]+JonesSols["Sols"]["t1"])/2.
            self.DicoJones['ra']=JonesSols['ClusterCat']['ra']
            self.DicoJones['dec']=JonesSols['ClusterCat']['dec']
            self.DicoJones['FreqDomains']=JonesSols['FreqDomains']
            self.DicoJones['FreqDomains_mean']=np.mean(JonesSols['FreqDomains'],axis=1)

        self.iCurrentMS+=1



    # def StackAll(self):
    #     while self.iCurrentMS<self.nMS:
    #         self.LoadNextMS()
    #         for iTime in range(self.NTimes):
    #             for iDir in range(self.NDir):
    #                 self.Stack_SingleTimeDir(iTime,iDir)
    #     self.Finalise()

    def StackAll(self):
        while self.iCurrentMS<self.nMS:
            if self.LoadNextMS()=="NotRead": continue
            print>>log,"Making dynamic spectra..."
            for iTime in range(self.NTimes):
                APP.runJob("Stack_SingleTime:%d"%(iTime), 
                           self.Stack_SingleTime,
                           args=(iTime,))#,serial=True)
            APP.awaitJobResults("Stack_SingleTime:*", progress="Append MS %i"%self.DicoDATA["iMS"])

            # for iTime in range(self.NTimes):
            #     self.Stack_SingleTime(iTime)
       
        self.Finalise()


    def killWorkers(self):
        print>>log, "Killing workers"
        APP.terminate()
        APP.shutdown()
        Multiprocessing.cleanupShm()



    def Finalise(self):
        self.killWorkers()

        G=self.DicoGrids["GridLinPol"]
        W=self.DicoGrids["GridWeight"].copy()
        W[W == 0] = 1
        Gn = G/W 
        self.Gn=Gn

        GOut=np.zeros_like(G)
        GOut[..., 0] =   0.5*(Gn[..., 0] + Gn[..., 3]) # I = 0.5(XX + YY)
        GOut[..., 1] =   0.5*(Gn[..., 0] - Gn[..., 3]) # Q = 0.5(XX - YY) 
        GOut[..., 2] =   0.5*(Gn[..., 1] + Gn[..., 2]) # U = 0.5(XY + YX)
        GOut[..., 3] = -0.5j*(Gn[..., 1] - Gn[..., 2]) # V = -0.5i(XY - YX)
        self.GOut = GOut

    def Stack_SingleTime(self,iTime):
        for iDir in range(self.NDir):
            self.Stack_SingleTimeDir(iTime,iDir)
        
    def Stack_SingleTimeDir(self,iTime,iDir):
        ra=self.PosArray.ra[iDir]
        dec=self.PosArray.dec[iDir]

        l, m = self.radec2lm(ra, dec)
        n  = np.sqrt(1. - l**2. - m**2.)
        self.DicoDATA.reload()
        self.DicoGrids.reload()
        indRow = np.where(self.DicoDATA["times"]==self.times[iTime])[0]
        #indRow = np.where(self.DicoDATA["times"]>0)[0]
        f   = self.DicoDATA["flag"][indRow, :, :]
        d   = self.DicoDATA["data"][indRow, :, :]
        A0s = self.DicoDATA["A0"][indRow]
        A1s = self.DicoDATA["A1"][indRow]
        u0  = self.DicoDATA["u"][indRow].reshape((-1,1,1))
        v0  = self.DicoDATA["v"][indRow].reshape((-1,1,1))
        w0  = self.DicoDATA["w"][indRow].reshape((-1,1,1))
        iMS  = self.DicoDATA["iMS"]


        chfreq=self.DicoMSInfos[iMS]["ChanFreq"].reshape((1,-1,1))
        chfreq_mean=np.mean(chfreq)
        # kk  = np.exp( -2.*np.pi*1j* f/const.c.value *(u0*l + v0*m + w0*(n-1)) ) # Phasing term
        #print iTime,iDir
        kk  = np.exp( -2.*np.pi*1j* chfreq/const.c.value *(u0*l + v0*m + w0*(n-1)) ) # Phasing term

        # #ind=np.where((A0s==0)&(A1s==10))[0]
        # ind=np.where((A0s!=1000))[0]
        # import pylab
        # pylab.ion()
        # pylab.clf()
        # pylab.plot(np.angle(d[ind,2,0]))
        # pylab.plot(np.angle(kk[ind,2,0].conj()))
        # pylab.draw()
        # pylab.show(False)
        # pylab.pause(0.1)

        
        f0, _ = self.Freq_minmax
        
        DicoMSInfos      = self.DicoMSInfos

        _,nch,_=self.DicoDATA["data"].shape

        dcorr=d
        if self.DoJonesCorr:
            self.DicoJones.reload()
            tm = self.DicoJones['tm']
            # Time slot for the solution
            iTJones=np.argmin(np.abs(tm-self.times[iTime]))
            iDJones=np.argmin(AngDist(ra,self.DicoJones['ra'],dec,self.DicoJones['dec']))
            iFJones=np.argmin(np.abs(chfreq_mean-self.DicoJones['FreqDomains_mean']))
            # construct corrected visibilities
            J0 = self.DicoJones['G'][iTJones, iFJones, A0s, iDJones, 0, 0]
            J1 = self.DicoJones['G'][iTJones, iFJones, A1s, iDJones, 0, 0]
            J0 = J0.reshape((-1, 1, 1))*np.ones((1, nch, 1))
            J1 = J1.reshape((-1, 1, 1))*np.ones((1, nch, 1))
            dcorr = J0.conj() * d * J1

        #ds=np.sum(d*kk, axis=0) # without Jones
        ds = np.sum(dcorr * kk, axis=0) # with Jones
        ws = np.sum(1-f, axis=0)

        ich0 = int( (self.DicoMSInfos[iMS]["ChanFreq"][0] - f0)/self.ChanWidth )
        self.DicoGrids["GridLinPol"][iDir,ich0:ich0+nch, iTime, :] = ds
        self.DicoGrids["GridWeight"][iDir,ich0:ich0+nch, iTime, :] = ws


    def NormJones(self, G):
        print>>log, "  Normalising Jones matrices by the amplitude"
        G[G != 0.] /= np.abs(G[G != 0.])
        return G
        


    def radec2lm(self, ra, dec):
        # ra and dec must be in radians
        l = np.cos(dec) * np.sin(ra - self.ra0)
        m = np.sin(dec) * np.cos(self.dec0) - np.cos(dec) * np.sin(self.dec0) * np.cos(ra - self.ra0)
        return l, m
# =========================================================================
# =========================================================================
