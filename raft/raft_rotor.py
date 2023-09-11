# RAFT's rotor class

import os
import yaml
import numpy as np
import matplotlib.pyplot as plt

from raft.pyIECWind         import pyIECWind_extreme
from raft.raft_member import Member

from scipy.interpolate      import PchipInterpolator
from scipy.special          import modstruve, iv


from raft.helpers                import rotationMatrix, getFromDict

try:
    from ccblade.ccblade import CCBlade, CCAirfoil
except:
    from wisdem.ccblade.ccblade import CCBlade, CCAirfoil


import pickle

if False:
    thrust_psd = pickle.load( open( "/Users/dzalkind/Tools/RAFT/designs/rotors/thrust_psd.p", "rb" ) )


# global constants
raft_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
rad2deg = 57.2958
rpm2radps = 0.1047

# a class for the rotor structure, aerodynamics, and control in RAFT
class Rotor:

    def __init__(self, turbine, w, ir):
        '''
        >>>> add mean offset parameters add move this to runCCBlade<<<<
        ir = the index of the values in the arrays of the input file for multiple rotors
        '''

        # Should inherit these from raft_model or _env?
        self.w = np.array(w)
        self.turbine = turbine      # store dictionary for later use

        self.coords = getFromDict(turbine, 'rotorCoords', dtype=list, shape=turbine['nrotors'], default=[[0,0]])[ir]
        
        self.nBlades    = getFromDict(turbine, 'nBlades', shape=turbine['nrotors'], dtype=int)[ir]         # [-]
        self.headings   = getFromDict(turbine, 'headings', shape=-1, default=[90,210,330])  # [deg]
        self.nBlades    = len(self.headings)

        self.axis       = getFromDict(turbine, 'axis', shape=turbine['nrotors'], default=[1,0,0])[ir]  # unit vector of rotor axis, facing downflow [-]

        self.Zhub       = getFromDict(turbine, 'hHub', shape=turbine['nrotors'])[ir]            # [m]
        self.Rhub       = getFromDict(turbine, 'Rhub', shape=turbine['nrotors'])[ir]            # [m]
        self.precone    = getFromDict(turbine, 'precone', shape=turbine['nrotors'])[ir]         # [m]
        self.shaft_tilt = getFromDict(turbine, 'shaft_tilt', shape=turbine['nrotors'])[ir]      # [deg]        
        self.overhang   = getFromDict(turbine, 'overhang', shape=turbine['nrotors'])[ir]        # [m]
        self.aeroServoMod = getFromDict(turbine, 'aeroServoMod', shape=turbine['nrotors'], default=1)[ir]  # flag for aeroservodynamics (0=none, 1=aero only, 2=aero and control)

        self.rHub = np.array([self.coords[0], self.coords[1], self.Zhub])  # save the rotor's hub coordinates in the platform reference frame [m]

        # support if blade and wt_ops are each a single dictionary or a list of dictionaries for each rotor
        # note: this would only be done on the first turbine  >>> we may want to move this stuff up a level, outside of each Rotor object
        if isinstance(turbine['blade'], dict):          # if we use the entire blade dict list approach
            turbine['blade'] = [turbine['blade']]*turbine['nrotors']
        if isinstance(turbine['wt_ops'], dict):
            turbine['wt_ops'] = [turbine['wt_ops']]*turbine['nrotors']

        self.R_rot      = getFromDict(turbine['blade'][ir], 'Rtip', shape=-1)
        # otherwise, we can avoid the if statment if we use the list approach within each dict value of the blade dict
        
        
        # Ensure the blade geometry inputs are inputted correctly
        nb = len(turbine['blade'])      # the number of blade types in the system.
            # Reminder: Assumption is that all blades on a rotor will be the same. You can input nrotors blade types for multiple rotors, or input one blade type and have it be used for all rotors in the system
        for ib in range(nb):
            nr = len(turbine['blade'][0]['geometry'])       # the number of nodes along the length of the blade
            r0 = turbine['blade'][ib]['geometry'][0][0]     # the first radius value of the blade
            rtip = turbine['blade'][ib]['geometry'][-1][0]  # the last radius value of the blade
            if r0 >= self.Rhub and rtip <= self.R_rot:  # if the geometry range lies between the hub radius and the blade length, do nothing, this is normal
                pass   
            #elif rtip-r0 == self.R_rot-self.Rhub and r0 < self.Rhub:     # if the geometry range is the right length, but starts inside the hub radius [0,length-Rhub], shift values to outside the hub [Rhub,length] 
                #print("WARNING: The input blade geometry radii start inside the hub, when they need to be defined outside of the hub")
                #for i in range(nr):
                    #turbine['blade'][ib]['geometry'][i][0] += self.Rhub-r0
            elif r0 < self.Rhub or rtip > self.R_rot:           # if none of the above works, then something is off with the input blade radii
                raise ValueError(f"Input blade geometry is invalid. First node radius needs to be >= Rhub ({self.Rhub}) or last node radius needs to be <= Rtip ({self.R_rot})")


        #yaw  = 0        

        self.Uhub      = getFromDict(turbine['wt_ops'][ir], 'v', shape=-1)
        self.Omega_rpm = getFromDict(turbine['wt_ops'][ir], 'omega_op', shape=-1)
        self.pitch_deg = getFromDict(turbine['wt_ops'][ir], 'pitch_op', shape=-1)

        self.I_drivetrain = getFromDict(turbine, 'I_drivetrain', shape=turbine['nrotors'])[ir]

        #self.aeroServoMod = getFromDict(turbine, 'aeroServoMod', default=1)  # flag for aeroservodynamics (0=none, 1=aero only, 2=aero and control)
        
		# Add parked pitch, rotor speed, assuming fully shut down by 40% above cut-out
        self.Uhub = np.r_[self.Uhub, self.Uhub.max()*1.4, 100]
        self.Omega_rpm = np.r_[self.Omega_rpm, 0, 0]
        self.pitch_deg = np.r_[self.pitch_deg, 90, 90]
		
        # Set default control gains
        self.kp_0 = np.zeros_like(self.Uhub)
        self.ki_0 = np.zeros_like(self.Uhub)       
        self.k_float = 0 # np.zeros_like(self.Uhub), right now this is a single value, but this may change    <<< what is this?


        # Set CCBlade flags
        tiploss = True # Tip loss model True/False
        hubloss = True # Hub loss model, True/False
        wakerotation = True # Wake rotation, True/False
        usecd = True # Use drag coefficient within BEMT, True/False


        # ----- AIRFOIL STUFF ------
        
        # compile info for airfoil station points along the blade
        #nStations = len(turbine['blade'][ir]["airfoils"])
        station_airfoil  = [ b for [a,b] in turbine['blade'][ir]["airfoils"] ]  # airfoil name
        station_position = [ a for [a,b] in turbine['blade'][ir]["airfoils"] ]  # airfoil relative position from blade root to tip [0-1]
        nStations = len(station_airfoil)
        
        # One fourth of the angles of attack from -pi to -pi/6, half between -pi/6 to pi/6, and one fourth from pi/6 to pi
        n_aoa = 200 # [-] - number of angles of attack to discretize airfoil polars - MUST BE MULTIPLE OF 4
        aoa = np.unique(np.hstack([np.linspace(-180, -30, int(n_aoa/4.0 + 1)), 
                                   np.linspace( -30,  30, int(n_aoa/2.0),),
                                   np.linspace(  30, 180, int(n_aoa/4.0 + 1))]))

        # compile info for individual airfoils
        n_af = len(turbine["airfoils"])  #len(np.unique(station_airfoil))  # number of airfoils that are used in the rotor
        airfoil_name = n_af * [""]         # name of each listed airfoil
        airfoil_thickness = np.zeros(n_af) # relative thickness of each listed airfoil (thickness/chord)
        Ca = np.zeros([n_af, 2])           # added mass coefficient [edgewise, flapwise] of each airfoil
        for i in range(n_af):
            airfoil_name[i] = turbine["airfoils"][i]["name"]
            airfoil_thickness[i] = turbine["airfoils"][i]["relative_thickness"]
            if 'added_mass_coeff' in turbine["airfoils"][i].keys():
                Ca[i,:] = turbine["airfoils"][i]["added_mass_coeff"]
            else:
                Ca[i,:] = [0.5, 1.0]  # default added mass coefficients if not supplied


        cl = np.zeros((n_af, n_aoa, 1))
        cd = np.zeros((n_af, n_aoa, 1))
        cm = np.zeros((n_af, n_aoa, 1))
        cpmin = np.zeros((n_af, n_aoa, 1))
        if len(np.array(turbine["airfoils"][i]['data'])[0]) > 4:
            cpmin_flag = True
        else:
            cpmin_flag = False

        # Interp cl-cd-cm along predefined grid of angle of attack
        for i in range(n_af):

            polar_table = np.array(turbine["airfoils"][i]['data'])
            
            # Note: polar_table[:,0] must be in degrees
            cl[i, :, 0] = np.interp(aoa, polar_table[:,0], polar_table[:,1])
            cd[i, :, 0] = np.interp(aoa, polar_table[:,0], polar_table[:,2])
            cm[i, :, 0] = np.interp(aoa, polar_table[:,0], polar_table[:,3])
            if cpmin_flag:
                cpmin[i, :, 0] = np.interp(aoa, polar_table[:,0], polar_table[:,4])
            

            #plt.figure()
            #plt.plot(polar_table[:,0], polar_table[:,1])
            #plt.plot(polar_table[:,0], polar_table[:,2])
            #plt.title(airfoil_name[i])
            
            if abs(cl[i, 0, 0] - cl[i, -1, 0]) > 1.0e-5:
                print("WARNING: Ai " + airfoil_name[i] + " has the lift coefficient different between + and - pi rad. This is fixed automatically, but please check the input data.")
                cl[i, 0, 0] = cl[i, -1, 0]
            if abs(cd[i, 0, 0] - cd[i, -1, 0]) > 1.0e-5:
                print("WARNING: Airfoil " + airfoil_name[i] + " has the drag coefficient different between + and - pi rad. This is fixed automatically, but please check the input data.")
                cd[i, 0, 0] = cd[i, -1, 0]
            if abs(cm[i, 0, 0] - cm[i, -1, 0]) > 1.0e-5:
                print("WARNING: Airfoil " + airfoil_name[i] + " has the moment coefficient different between + and - pi rad. This is fixed automatically, but please check the input data.")
                cm[i, 0, 0] = cm[i, -1, 0]
            if cpmin_flag and abs(cpmin[i, 0, 0] - cpmin[i, -1, 0]) > 1.0e-5:
                print("WARNING: Airfoil " + airfoil_name[i] + " has the minimum pressure coefficient different between + and - pi rad. This is fixed automatically, but please check the input data.")
                cpmin[i, 0, 0] = cpmin[i, -1, 0]



        # Set discretization parameters
        nSector = getFromDict(turbine['blade'][ir], 'nSector', default=4) # number of equally spaced azimuthal positions for CCblade to compute and average over
        nr = getFromDict(turbine['blade'][ir], 'nr', default=20) # number of radial blade stations (or blade elements) to use
        
        grid = np.linspace(0., 1., nr, endpoint=False) + 0.5/nr # equally spaced grid along blade span, root=0 tip=1


        # ----- Interpolate airfoil coefficients over the blade span using a pchip on relative thickness -----
        
        station_thickness = np.zeros(nStations)
        station_Ca = np.zeros((nStations, 2))
        station_cl = np.zeros((nStations, n_aoa, 1))
        station_cd = np.zeros((nStations, n_aoa, 1))
        station_cm = np.zeros((nStations, n_aoa, 1))
        station_cpmin = np.zeros((nStations, n_aoa, 1))

        # copy-paste coefficient values from airfoil database to each station point along the blade
        for i in range(nStations):
            for j in range(n_af):
                if station_airfoil[i] == airfoil_name[j]:
                    station_thickness[i] = airfoil_thickness[j]
                    station_Ca[i,:] = Ca[j,:]
                    station_cl[i, :, :] = cl[j, :, :]
                    station_cd[i, :, :] = cd[j, :, :]
                    station_cm[i, :, :] = cm[j, :, :]
                    station_cpmin[i, :, :] = cpmin[j, :, :]
                    break

        if np.all(station_thickness == np.flip(sorted(station_thickness))):  # if the airfoils get consistently thinner toward the tip

            # Spanwise interpolation of the airfoil polars with a pchip
            spline = PchipInterpolator  # select spline interpolation method
            
            # spline interpolate airfoil thickness over evenly spaced element locations along span
            rthick_spline = spline(station_position, station_thickness)
            self.r_thick_interp = rthick_spline(grid) 
            
            # make nonredundant (and sorted) list of airfoil thicknesses (and indices)
            r_thick_unique, indices = np.unique(station_thickness, return_index=True)
            
            Ca_spline = spline(station_position, station_Ca)
            self.Ca_interp = Ca_spline(grid)
            
            cl_spline = spline(r_thick_unique, station_cl[indices, :, :])
            self.cl_interp = np.flip(cl_spline(np.flip(self.r_thick_interp)), axis=0)
            
            cd_spline = spline(r_thick_unique, station_cd[indices, :, :])
            self.cd_interp = np.flip(cd_spline(np.flip(self.r_thick_interp)), axis=0)
            
            cm_spline = spline(r_thick_unique, station_cm[indices, :, :])
            self.cm_interp = np.flip(cm_spline(np.flip(self.r_thick_interp)), axis=0)
            
            cpmin_spline = spline(r_thick_unique, station_cpmin[indices, :, :])
            self.cpmin_interp = np.flip(cpmin_spline(np.flip(self.r_thick_interp)), axis=0)
        
        else:  # if it's an atypical case with non-ordered airfoil thicknesses
            # do simple span-based interpolation
            breakpoint()
            self.Ca_interp    = np.interp(grid, station_position, station_Ca)
            self.cl_interp    = np.interp(grid, station_position, station_cl)
            self.cd_interp    = np.interp(grid, station_position, station_cd)
            self.cm_interp    = np.interp(grid, station_position, station_cm)
            self.cpmin_interp = np.interp(grid, station_position, station_cpmin)
        
        self.aoa = aoa
        
        # split out blade geometry info from table 
        geometry_table = np.array(turbine['blade'][ir]['geometry'])
        r_input           = geometry_table[:,0]
        self.dr = (rtip - self.Rhub)/nr
        
        # radial locations of blade elements for BEM
        self.blade_r      = np.linspace(self.Rhub, rtip, nr, endpoint=False) + self.dr/2  
        
        self.blade_chord  = np.interp(self.blade_r, r_input, geometry_table[:,1])
        self.blade_theta  = np.interp(self.blade_r, r_input, geometry_table[:,2])
        blade_precurve    = np.interp(self.blade_r, r_input, geometry_table[:,3])
        blade_presweep    = np.interp(self.blade_r, r_input, geometry_table[:,4])
        #  <<<<<< move this to beginning, then do some interpolating to unify grid and blade_r <<<<<<< and go from above 0 to below 1

        if self.Zhub < 0:
            self.rho = turbine['rho_water']
            self.mu = turbine['mu_water']
            self.shearExp = turbine['shearExp_water']
        else:
            self.rho = turbine['rho_air']
            self.mu = turbine['mu_air']
            self.shearExp = turbine['shearExp_air']
        
        af = []
        for i in range(self.cl_interp.shape[0]):
            af.append(CCAirfoil(self.aoa, [], self.cl_interp[i,:,:],self.cd_interp[i,:,:],self.cm_interp[i,:,:]))
        
        # >>> There is an inconsistency between the geometric and airfoil inputs that needs to be corrected! <<<
        
        self.ccblade = CCBlade(
            self.blade_r,                        # (m) locations defining the blade along z-axis of blade coordinate system
            self.blade_chord,                    # (m) corresponding chord length at each section
            self.blade_theta,                    # (deg) corresponding :ref:`twist angle <blade_airfoil_coord>` at each section---positive twist decreases angle of attack.
            af,                             # CCAirfoil object
            self.Rhub,                      # (m) radius of hub
            turbine['blade'][ir]['Rtip'],   # (m) radius of tip
            self.nBlades,                   # number of blades
            self.rho,                       # (kg/m^3) freestream fluid density
            self.mu,                        # (kg/m/s) dynamic viscosity of fluid
            self.precone,                   # (deg) hub precone angle
            self.shaft_tilt,                # (deg) hub tilt angle
            0.0,                            # (deg) nacelle yaw angle
            self.shearExp,                  # shear exponent for a power-law wind profile across hub
            self.Zhub,                      # (m) hub height used for power-law wind profile.  U = Uref*(z/hubHt)**shearExp
            nSector,                        # number of azimuthal sectors to descretize aerodynamic calculation.  automatically set to 1 if tilt, yaw, and shearExp are all 0.0.  Otherwise set to a minimum of 4.
            blade_precurve,                 # (m) location of blade pitch axis in x-direction of :ref:`blade coordinate system <azimuth_blade_coord>`
            turbine['blade'][ir]['precurveTip'],# (m) location of blade pitch axis in x-direction at the tip (analogous to Rtip)
            blade_presweep,                 # (m) location of blade pitch axis in y-direction of :ref:`blade coordinate system <azimuth_blade_coord>`
            turbine['blade'][ir]['presweepTip'],# (m) location of blade pitch axis in y-direction at the tip (analogous to Rtip)
            tiploss=tiploss,                # if True, include Prandtl tip loss model
            hubloss=hubloss,                # if True, include Prandtl hub loss model
            wakerotation=wakerotation,      # if True, include effect of wake rotation (i.e., tangential induction factor is nonzero)
            usecd=usecd,                    # If True, use drag coefficient in computing induction factors (always used in evaluating distributed loads from the induction factors).
            derivatives=True,               # if True, derivatives along with function values will be returned for the various methods
        )
        
        # pull control gains out of dictionary
        self.setControlGains(turbine)

        # create a member list of blade sections, only if rotor is underwater
        if self.Zhub + self.R_rot < 0:
            #self.bladeAirfoil2Member()
            self.bladeGeometry2Member()
        else:
            self.bladeMemberList = []
    
    """
    def bladeAirfoil2Member(self, Ca_edge=0.5, Ca_flap=1.0):
        '''First iteration of a method to create RAFT members for the rotor blades (not used right now).

        Method to create members for each airfoil in the turbine blade
        To be used for added mass and buoyancy calculations of underwater turbines'''
        
        self.bladeMemberList = []
        blade_length = self.R_rot-self.Rhub
        blade_r = np.array(station_position)*blade_length

        airfoil_name_dict = [foil['name'] for foil in self.turbine['airfoils']]

        for i,af in enumerate(station_airfoil[:-1]):
            airfoil = {}        # dictionary to hold properties of blade sub-member = each airfoil
            airfoil['name'] = af+'-'+str(i+1)+'/'+str(len(station_airfoil))
            airfoil['type'] = 3

            # started a fancy method to determine the axis that the airfoil blades will have different headings about
            # ideally, I want to find a vector (r) orthogonal to the rotor axis vector (n), which has infinite solutions (r dot n = 0)
            # the way I set it up below rotates the rotor axis vector 90 degrees, but breaks down if rotor.axis[2] != 0 since the rotation metrix used here is about the z axis
            airfoil_zero_heading = np.matmul(np.array([[0, -1, 0],[1, 0, 0],[0, 0, 1]]), self.axis)
            airfoil['rA'] = np.array(airfoil_zero_heading)*(self.Rhub+(station_position[i]*blade_length))
            airfoil['rB'] = airfoil['rA'] + np.array(airfoil_zero_heading)*((station_position[i+1]-station_position[i])*blade_length)

            #airfoil['rA'] = rHub + np.array([0,0,self.Rhub]) + np.array([0,0,(station_position[i])*blade_length])
            #airfoil['rB'] = airfoil['rA'] + np.array([0,0, (station_position[i+1]-station_position[i])*blade_length])
            # >>>>>>>> don't need to specify direction of blade; just assume vertical and then can transform in later operations <<<<<<<<<<<<<
            airfoil['shape'] = 'rect'
            airfoil['stations'] = [0,1]

            chord = np.interp(blade_r[i], self.blade_r, self.blade_chord)
            rel_t = self.turbine["airfoils"][airfoil_name_dict.index(af)]["relative_thickness"]
            A = (np.pi/4)*chord**2 * rel_t
            sideB = A/chord     # the length of the imaginary side length of the rectange that gives the same area

            airfoil['d'] = [chord, sideB]
            #airfoil['d'] = np.interp(blade_r[i:i+2], self.blade_r, self.blade_chord)
            airfoil['gamma'] = np.interp(blade_r[i], self.blade_r, self.blade_theta)
            airfoil['potMod'] = False

            airfoil['Cd'] = 0.0
            if 'added_mass_coeff' in self.turbine["airfoils"][airfoil_name_dict.index(af)]:
                added_mass_coeff = self.turbine["airfoils"][airfoil_name_dict.index(af)]['added_mass_coeff']
            else:
                added_mass_coeff = [Ca_edge, Ca_flap]
            airfoil['Ca'] = added_mass_coeff 
            #airfoil['Ca'] = self.turbine["airfoils"][airfoil_name_dict.index(af)]["added_mass_coeff"]
            airfoil['CdEnd'] = 0.0
            airfoil['CaEnd'] = 0.0
        
            airfoil['t'] = 0.01
            airfoil['rho_shell'] = 1850

            self.bladeMemberList.append(Member(airfoil, len(self.w)))
    """


    def bladeGeometry2Member(self):
        '''Second iteration of a function to create RAFT members based on rotor blades (is currently used).

        Method to create members for each "node" that is specified in turbine['blade']['geometry']
        To be used for added mass and buoyancy calculations of underwater turbines'''

        self.bladeMemberList = []

        for i in range(len(self.blade_r)-1):
            blademem = {}
            blademem['name'] = i
            blademem['type'] = 3

            airfoil_zero_heading = np.matmul(np.array([[0, -1, 0],[1, 0, 0],[0, 0, 1]]), self.axis) # see comments in bladeAirfoil2Member()
            blademem['rA'] = np.array(airfoil_zero_heading) * (self.blade_r[i] - self.dr/2)
            blademem['rB'] = np.array(airfoil_zero_heading) * (self.blade_r[i] + self.dr/2)

            blademem['shape'] = 'rect'
            blademem['stations'] = [0,1]

            chord = self.blade_chord[i]
            rel_thick = self.r_thick_interp[i]
            area = (np.pi/4)*chord**2 * rel_thick
            rect_thick = area/chord  # thickness of rectange with same chord length to achieve same cross sectional area
            blademem['d'] = [[chord, rect_thick],[chord, rect_thick]]

            blademem['gamma'] = self.blade_theta[i]

            blademem['potMod'] = False

            blademem['Cd'] = 0.0
            blademem['Ca'] = self.Ca_interp[i,:]            
            blademem['CdEnd'] = 0.0
            blademem['CaEnd'] = 0.0
        
            blademem['t'] = 0.01
            blademem['rho_shell'] = 1850

            self.bladeMemberList.append(Member(blademem, len(self.w)))
        
        self.nodes = np.zeros([int(self.nBlades), len(self.bladeMemberList)+1, 3])      # array to hold xyz positions of each node along a blade for each blade (filled in later)


    def getBladeMemberPositions(self, azimuth, r_OG):
        ''' Returns the node positions of blade members as it is rotated by an azimuth angle about the rotor's axis.
        rOG is a matrix of n number of rows and 3 columns, where each row is a position vector that needs rotating'''

        # create rotation matrix based on the rotor's axis (default axis=[1,0,0])
        c = np.cos(np.deg2rad(azimuth))
        s = np.sin(np.deg2rad(azimuth))
        a = self.axis  # each rotor is given a default axis of rotation about the x-direction
        R = np.array([[c + a[0]**2*(1-c), a[0]*a[1]*(1-c)-a[2]*s, a[0]*a[2]*(1-c)+a[1]*s],
                        [a[1]*a[0]*(1-c)+a[2]*s, c + a[1]**2*(1-c), a[1]*a[2]*(1-c)-a[0]*s],
                        [a[2]*a[0]*(1-c)-a[1]*s, a[2]*a[1]*(1-c)+a[0]*s, c + a[2]**2*(1-c)]])
        #rotMatx = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])

        # find the new node positions of the blade member
        r_new = np.zeros_like(r_OG)
        for i in range(len(r_OG)):
            r_from_Zhub = np.matmul(R, r_OG[i,:])   # wrt to the hub center
            r_new[i,:] = r_from_Zhub + self.rHub    # wrt to the global coordinates

        return r_new



    def calcCavitation(self, case, azimuth=0, clearance_margin=1.0, Patm=101325, Pvap=2500, error_on_cavitation=False):
        ''' Method to calculate the cavitation number of the rotor
        (wind speed (m/s), rotor speed (RPM), pitch angle (deg), azimuth (deg))

        Can later move Patm and Pvap to some kind of input file
        '''
        # -------------- calculate worst case clearance below waterline (less precise) ---------------
        # calculate the worst-case scenario depth below the free surface where cavitation can occur
        if self.Zhub < 0:
            clearance = self.Zhub + self.R_rot
        else:
            raise ValueError("Hub Depth must be below the water surface to calculate cavitation")
        # add a margin to the depth clearance (either by user input or based on platform motions)
        clearance = clearance*clearance_margin
        
        # >>> note: above currently not used <<<
        
        #--------------- calculate clearance (depth) of each node of each blade (more precise) --------------
        # collect minimum pressure coefficient values
        cpmin = self.cpmin_interp       # array of size [len(bladeMemberList), len(self.aoa), 1] where each row is the cpmin as a function of aoa (columns)
        
        # set wind speed, rotor speed, and blade pitch angle based on wind speed an turbine inputs
        Uhub = case['current_speed']
        Omega_rpm = np.interp(Uhub, self.Uhub, self.Omega_rpm)  # rotor speed [rpm]
        pitch_deg = np.interp(Uhub, self.Uhub, self.pitch_deg)  # blade pitch angle [deg]

        # create array to store cavitation values for each node for each blade
        cav_check = np.zeros([len(self.headings), len(self.blade_r)])

        # calculate the critial sigma caviatation parameter for each blade node and compare to the sigma_l caviatation parameter to determine if cavitation occurs
        for a,azi in enumerate(self.headings):      # do this for each blade (aoa and relative velocity change for different blade azimuth angles)

            loads, derivs = self.ccblade.distributedAeroLoads(Uhub, Omega_rpm, pitch_deg, azi)  # run CCBlade with variable azimuth angles
            vrel = loads["W"]       # pull out the relative velocity at each node along the blade at that azimuth angle
            aoa = loads["alpha"]    # pull out the angle of attack at each node along the blade at that azimuth angle
            
            for n in range(len(vrel)):      # for each blade node

                # find the minimum pressure coefficient at that node at the given angle of attack
                cpmin_node = np.interp(aoa[n], self.aoa, cpmin[n,:,0])

                # extract the depth of the node using the node position array
                clearance = self.nodes[a, n, 2]     # a=which blade, n=which node, 2=z-position=depth
                
                # calculate the critial sigma cavitation parameter
                sigma_crit = (Patm + self.ccblade.rho*9.81*abs(clearance) - Pvap)/(0.5*self.ccblade.rho*vrel[n]**2)

                # if sigma_crit is less than sigma_l (sigma_l = -cpmin), then cavitation occurs
                if error_on_cavitation:
                    if sigma_crit < -cpmin_node:
                        raise ValueError(f"Cavitation occured at node {n} (first node = 0)")
                
                cav_check[a,n] = sigma_crit + cpmin_node         # if this value is negative, then cavitation occurs (sigma_crit - sigma_l < 0 -> cav occurs; sigma_l = -cpmin_node)


        return cav_check


    def runCCBlade(self, Uhub, ptfm_pitch=0, yaw_misalign=0):
        '''This performs a single CCBlade evaluation at specified conditions.
        
        ptfm_pitch
            mean platform pitch angle to be included in rotor tilt angle [rad]
        yaw_misalign
            turbine yaw misalignment angle [deg]
        '''
        
        # find turbine operating point at the provided wind speed
        Omega_rpm = np.interp(Uhub, self.Uhub, self.Omega_rpm)  # rotor speed [rpm]
        pitch_deg = np.interp(Uhub, self.Uhub, self.pitch_deg)  # blade pitch angle [deg]
        
        # adjust rotor angles based on provided info (I think this intervention in CCBlade should work...)
        self.ccblade.tilt = np.deg2rad(self.shaft_tilt) + ptfm_pitch
        self.ccblade.yaw  = np.deg2rad(yaw_misalign)
        
        # evaluate aero loads and derivatives with CCBlade
        loads, derivs = self.ccblade.evaluate(Uhub, Omega_rpm, pitch_deg, coefficients=True)
        
        # organize and save the relevant outputs...
        self.U_case         = Uhub
        self.Omega_case     = Omega_rpm
        self.aero_torque    = loads["Q"][0]
        self.aero_power     = loads["P"][0]
        self.aero_thrust    = loads["T"][0]
        self.pitch_case     = pitch_deg

        outputs = {}
        
        outputs["P"] = loads["P"]
        outputs["Mb"] = loads["Mb"]
        outputs["CP"] = loads["CP"]
        outputs["CMb"] = loads["CMb"]
        outputs["Fhub"] = np.array( [loads["T" ][0], loads["Y"  ][0], loads["Z"  ][0]])
        outputs["Mhub"] = np.array( [loads["Q" ][0], loads["My" ][0], loads["Mz" ][0]])
        outputs["CFhub"] = np.array([loads["CT"][0], loads["CY" ][0], loads["CZ" ][0]])
        outputs["CMhub"] = np.array([loads["CQ"][0], loads["CMy"][0], loads["CMz"][0]])


        print(f"Wind speed: {Uhub:.2f} m/s, Omega: {Omega_rpm:.2f} rpm, Cp: {loads['CP'][0]:4.3f}, T: {loads['T'][0]/1e3:.0f} kN")
        
        J={} # Jacobian/derivatives

        dP = derivs["dP"]
        J["P", "r"] = dP["dr"]
        # J["P", "chord"] = dP["dchord"]
        # J["P", "theta"] = dP["dtheta"]
        # J["P", "Rhub"] = np.squeeze(dP["dRhub"])
        # J["P", "Rtip"] = np.squeeze(dP["dRtip"])
        # J["P", "hub_height"] = np.squeeze(dP["dhubHt"])
        # J["P", "precone"] = np.squeeze(dP["dprecone"])
        # J["P", "tilt"] = np.squeeze(dP["dtilt"])
        # J["P", "yaw"] = np.squeeze(dP["dyaw"])
        # J["P", "shearExp"] = np.squeeze(dP["dshear"])
        # J["P", "V_load"] = np.squeeze(dP["dUinf"])
        # J["P", "Omega_load"] = np.squeeze(dP["dOmega"])
        # J["P", "pitch_load"] = np.squeeze(dP["dpitch"])
        # J["P", "precurve"] = dP["dprecurve"]
        # J["P", "precurveTip"] = dP["dprecurveTip"]
        # J["P", "presweep"] = dP["dpresweep"]
        # J["P", "presweepTip"] = dP["dpresweepTip"]

        dQ = derivs["dQ"]
        J["Q","Uhub"]      = np.atleast_1d(np.diag(dQ["dUinf"]))
        J["Q","pitch_deg"] = np.atleast_1d(np.diag(dQ["dpitch"]))
        J["Q","Omega_rpm"] = np.atleast_1d(np.diag(dQ["dOmega"]))

        dT = derivs["dT"]
        J["T","Uhub"]      = np.atleast_1d(np.diag(dT["dUinf"]))
        J["T","pitch_deg"] = np.atleast_1d(np.diag(dT["dpitch"]))
        J["T","Omega_rpm"] = np.atleast_1d(np.diag(dT["dOmega"]))

        # dT = derivs["dT"]
        # .J["Fhub", "r"][0,:] = dT["dr"]     # 0 is for thrust force, 1 would be y, 2 z
        # .J["Fhub", "chord"][0,:] = dT["dchord"]
        # .J["Fhub", "theta"][0,:] = dT["dtheta"]
        # .J["Fhub", "Rhub"][0,:] = np.squeeze(dT["dRhub"])
        # .J["Fhub", "Rtip"][0,:] = np.squeeze(dT["dRtip"])
        # .J["Fhub", "hub_height"][0,:] = np.squeeze(dT["dhubHt"])
        # .J["Fhub", "precone"][0,:] = np.squeeze(dT["dprecone"])
        # .J["Fhub", "tilt"][0,:] = np.squeeze(dT["dtilt"])
        # .J["Fhub", "yaw"][0,:] = np.squeeze(dT["dyaw"])
        # .J["Fhub", "shearExp"][0,:] = np.squeeze(dT["dshear"])
        # .J["Fhub", "V_load"][0,:] = np.squeeze(dT["dUinf"])
        # .J["Fhub", "Omega_load"][0,:] = np.squeeze(dT["dOmega"])
        # .J["Fhub", "pitch_load"][0,:] = np.squeeze(dT["dpitch"])
        # .J["Fhub", "precurve"][0,:] = dT["dprecurve"]
        # .J["Fhub", "precurveTip"][0,:] = dT["dprecurveTip"]
        # .J["Fhub", "presweep"][0,:] = dT["dpresweep"]
        # .J["Fhub", "presweepTip"][0,:] = dT["dpresweepTip"]

        self.J = J

        return loads, derivs        


    def setControlGains(self,turbine):
        '''
        Use flipped sign version of ROSCO
        '''

        # Convert gain-scheduling wrt pitch to wind speed, Add zero gains for parked "control"
        pc_angles = np.array(turbine['pitch_control']['GS_Angles']) * rad2deg
        self.kp_0 = np.interp(self.pitch_deg,pc_angles,turbine['pitch_control']['GS_Kp'],left=0,right=0)
        self.ki_0 = np.interp(self.pitch_deg,pc_angles,turbine['pitch_control']['GS_Ki'],left=0,right=0)
        self.k_float = -turbine['pitch_control']['Fl_Kp']

        # Torque control
        self.kp_tau = -turbine['torque_control']['VS_KP']
        self.ki_tau = -turbine['torque_control']['VS_KI']
        self.Ng     = turbine['gear_ratio']
            


    def calcAeroServoContributions(self, case, ptfm_pitch=0, current=False, display=0):
        '''Calculates stiffness, damping, added mass, and excitation coefficients
        from rotor aerodynamics coupled with turbine controls.
        Results are w.r.t. the hub coordinate on the nacelle reference frame (may be yawed)
        Currently returning 6 DOF mean loads, but other terms are just hub fore-aft scalars.
        
         ptfm_pitch
            mean platform pitch angle to be included in rotor tilt angle [rad]
        '''
        
        # get relative inflow angle
        if current:
            speed = getFromDict(case, 'current_speed', shape=0, default=1.0)
            heading = getFromDict(case, 'current_heading', shape=0, default=0.0)
        else:
            speed = getFromDict(case, 'wind_speed', shape=0, default=10)
            heading = getFromDict(case, 'wind_heading', shape=0, default=0.0)
            
        turbine_heading = getFromDict(case, 'turbine_heading', shape=0, default=0.0)  # [deg]
        #turbine_heading = self.turbine_heading  #<<<<  USE THIS IN FUTURE <<<<<<
        yaw_misalign = heading - turbine_heading  # inflow misalignment heading relative to turbine heading [deg]

        # call CCBlade
        loads, derivs = self.runCCBlade(speed, ptfm_pitch=ptfm_pitch, yaw_misalign=yaw_misalign)
        
        #Uinf = case['wind_speed']  # inflow wind speed (m/s) <<< eventually should be consistent with rest of RAFT
        Uinf = speed
        
        # extract derivatives of interest
        dT_dU  = np.atleast_1d(np.diag(derivs["dT"]["dUinf"]))
        dT_dOm = np.atleast_1d(np.diag(derivs["dT"]["dOmega"])) / rpm2radps
        dT_dPi = np.atleast_1d(np.diag(derivs["dT"]["dpitch"])) * rad2deg
        dQ_dU  = np.atleast_1d(np.diag(derivs["dQ"]["dUinf"]))
        dQ_dOm = np.atleast_1d(np.diag(derivs["dQ"]["dOmega"])) / rpm2radps
        dQ_dPi = np.atleast_1d(np.diag(derivs["dQ"]["dpitch"])) * rad2deg

        # calculate steady aero forces and moments
        F_aero0 = np.array([loads["T" ][0], loads["Y"  ][0], loads["Z"  ][0],
                            loads["My" ][0], loads["Q" ][0], loads["Mz" ][0] ])
        
        #>>> need a rotor heading rotation somewhere applied to the forces <<<<
        
        # calculate rotor-averaged turbulent wind spectrum
        _,_,_,S_rot = self.IECKaimal(case, current=current)   # PSD [(m/s)^2/rad]
        self.V_w = np.sqrt(S_rot)   # convert from power spectral density to complex amplitudes (FFT)


        # no-control option
        if self.aeroServoMod == 1:  

            a_aero = np.zeros(len(self.w)) 
            b_aero = np.zeros(len(self.w)) + dT_dU

            f_aero =  dT_dU * np.sqrt(S_rot)
            
        # control option
        elif self.aeroServoMod == 2:  
        
            # Pitch control gains at Uinf (Uinf), flip sign to translate ROSCO convention to this one
            self.kp_beta    = -np.interp(Uinf, self.Uhub, self.kp_0) 
            self.ki_beta    = -np.interp(Uinf, self.Uhub, self.ki_0) 

            # Torque control gains, need to get these from somewhere
            kp_tau = self.kp_tau * (self.kp_beta == 0)  #     -38609162.66552     ! VS_KP				- Proportional gain for generator PI torque controller [1/(rad/s) Nm]. (Only used in the transitional 2.5 region if VS_ControlMode =/ 2)
            ki_tau = self.kp_tau  * (self.kp_beta == 0)   #    -4588245.18720      ! VS_KI	
            
            a_aer = np.zeros_like(self.w)
            b_aer = np.zeros_like(self.w)
            C   = np.zeros_like(self.w,dtype=np.complex_)
            C2  = np.zeros_like(self.w,dtype=np.complex_)
            D   = np.zeros_like(self.w,dtype=np.complex_)
            E   = np.zeros_like(self.w,dtype=np.complex_)

            # Roots of characteristic equation, helps w/ debugging
            # p = np.array([-self.I_drivetrain, (dQ_dOm + self.kp_beta * dQ_dPi - self.Ng * kp_tau), self.ki_beta* dQ_dPi - self.Ng * ki_tau])
            # r = np.roots(p)

            for iw, omega in enumerate(self.w):
                
                # Denominator of control transfer function
                D[iw] = self.I_drivetrain * omega**2 + (dQ_dOm + self.kp_beta * dQ_dPi - self.Ng * kp_tau) * 1j * omega + self.ki_beta* dQ_dPi - self.Ng * ki_tau

                # control transfer function
                C[iw] = 1j * omega * (dQ_dU - self.k_float * dQ_dPi / self.Zhub) / D[iw]

                # Thrust transfer function
                E[iw] = ((dT_dOm + self.kp_beta * dT_dPi) * 1j * omega + self.ki_beta * dT_dPi )

                # alternative for debugging
                C2[iw] = C[iw] / (1j * omega)

                # Complex aero damping
                T = 1j * omega * (dT_dU - self.k_float * dT_dPi / self.Zhub) - ( E[iw] * C[iw])
                
                # Aerodynamic coefficients
                a_aer[iw] = -(1/omega**2) * np.real(T)
                b_aer[iw] = (1/omega) * np.imag(T)
            
            # Save transfer functions required for output
            self.C = C
            
            # calculate wind excitation force/moment spectra
            T_0 = loads["T" ][0]
            T_w1 = dT_dU * self.V_w
            T_w2 = (E * C * self.V_w) / (1j * self.w) * (-1)  # mhall: think this needs the sign reversal

            T_ext = T_w1 + T_w2


            # --- new approach ---
            
            
            # transfer function from torque to thrust                
            H_QT = ((dT_dOm + self.kp_beta*dT_dPi)*1j*self.w + self.ki_beta*dT_dPi) / (
                   self.I_drivetrain*self.w**2 + (dQ_dOm + self.kp_beta*dQ_dPi - self.Ng*kp_tau)*1j*self.w + self.ki_beta*dQ_dPi - self.Ng*ki_tau )

            # save excitation coefficient
            self.c_exc = dT_dU - H_QT*dQ_dU

            f2 = (dT_dU - H_QT*dQ_dU) * self.V_w  # excitation force
            b2 = np.real(  dT_dU - self.k_float*dT_dPi - H_QT*(dQ_dU - self.k_float*dQ_dPi)             )  # damping
            a2 = np.real( (dT_dU - self.k_float*dT_dPi - H_QT*(dQ_dU - self.k_float*dQ_dPi))/(1j*self.w))  # added mass

            # without nacelle feedback
            b3 = np.real(  dT_dU - H_QT*dQ_dU             )  # damping
            a3 = np.real( (dT_dU - H_QT*dQ_dU)/(1j*self.w))  # added mass


            if display > 1:
                '''
                plt.plot(self.w/2/np.pi, self.V_w, label = 'S_rot')
                plt.yscale('log')
                plt.xscale('log')

                plt.xlim([1e-2,10])
                plt.grid('True')

                plt.xlabel('Freq. (Hz)')
                plt.ylabel('PSD')

                #plt.plot(thrust_psd.fq_0 * 2 * np.pi,thrust_psd.psd_0)r
                plt.plot(self.w, np.abs(T_ext))
                plt.plot(self.w, abs(T_w2))
                '''
                
                fig,ax = plt.subplots(4,1,sharex=True)
                ax[0].plot(self.w/2.0/np.pi, self.V_w);  ax[0].set_ylabel('U (m/s)') 
                ax[1].plot(self.w/2.0/np.pi, T_w1    );  ax[1].set_ylabel('T_w1') 
                ax[2].plot(self.w/2.0/np.pi, np.real(T_w2),'k')
                ax[2].plot(self.w/2.0/np.pi, np.imag(T_w2),'k:'); ax[2].set_ylabel('T_w2') 
                ax[3].plot(self.w/2.0/np.pi, np.real(T_w1+T_w2),'k')
                ax[3].plot(self.w/2.0/np.pi, np.imag(T_w1+T_w2),'k:'); ax[3].set_ylabel('T_w2+T_w2') 
                ax[3].set_xlabel('f (Hz)') 
                
                
                fig,ax = plt.subplots(4,1,sharex=True)
                ax[0].plot(self.w/2.0/np.pi, self.V_w);  ax[0].set_ylabel('U (m/s)') 
                ax[1].plot(self.w/2.0/np.pi, T_w1 , 'g--')
                ax[1].plot(self.w/2.0/np.pi, T_w2 , 'g:')
                ax[1].plot(self.w/2.0/np.pi, T_ext, 'g')
                ax[1].plot(self.w/2.0/np.pi, dT_dU*self.V_w, 'k--')
                ax[1].plot(self.w/2.0/np.pi,-H_QT*dQ_dU*self.V_w , 'k:')
                ax[1].plot(self.w/2.0/np.pi, f2        , 'k'  );  ax[1].set_ylabel('F') 
                ax[2].plot(self.w/2.0/np.pi, b_aer     , 'g')
                ax[2].plot(self.w/2.0/np.pi, b3        , 'b')
                ax[2].plot(self.w/2.0/np.pi, b2        , 'k--');  ax[2].set_ylabel('B') 
                ax[3].plot(self.w/2.0/np.pi, a_aer     , 'g')
                ax[3].plot(self.w/2.0/np.pi, a3        , 'b')
                ax[3].plot(self.w/2.0/np.pi, a2        , 'k--');  ax[3].set_ylabel('A') 
                ax[3].set_xlabel('f (Hz)') 

                plt.show()
                

            f_aero = f2  # wind thrust force excitation spectrum
            a_aero = a2
            b_aero = b2
        
        return F_aero0, f_aero, a_aero, b_aero #  B_aero, C_aero, F_aero0, F_aero
        
        
    def plot(self, ax, r_ptfm=[0,0,0], R_ptfm=np.eye(3), azimuth=0, color='k', airfoils=False):
        '''Draws the rotor on the passed axes, considering optional platform offset and rotation matrix, and rotor azimuth angle'''

        # ----- blade geometry ----------

        m = len(self.ccblade.chord)

        # lists to be filled with coordinates for plotting
        X = []
        Y = []
        Z = []        
        
        # generic airfoil for now
        afx = np.array([ 0.0 , -0.16, 0.0 ,  0.0 ])
        afy = np.array([-0.25,  0.  , 0.75, -0.25])
        npts = len(afx)
        
        # should add real airfoil shapes, and twist     
        for i in range(m):
            for j in range(npts):
                X.append(self.ccblade.chord[i]*afx[j])
                Y.append(self.ccblade.chord[i]*afy[j])
                Z.append(self.ccblade.r[i])            
                #X.append(self.ccblade.chord[i+1]*afx[j])
                #Y.append(self.ccblade.chord[i+1]*afy[j])
                #Z.append(self.ccblade.r[i+1]) 
                
        P = np.array([X, Y, Z])
        
        # ----- rotation matricse ----- 
        # (blade pitch would be a -rotation about local z)
        R_precone = rotationMatrix(0, -self.ccblade.precone, 0)  
        R_azimuth = [rotationMatrix(azimuth + azi, 0, 0) for azi in 2*np.pi/3.*np.arange(3)]
        R_tilt    = rotationMatrix(0, np.deg2rad(self.shaft_tilt), 0)   # # define x as along shaft downwind, y is same as ptfm y
        
        # ----- transform coordinates -----
        for ib in range(3):
        
            P2 = np.matmul(R_precone, P)
            P2 = np.matmul(R_azimuth[ib], P2)
            P2 = np.matmul(R_tilt, P2)
            P2 = P2 + np.array([-self.overhang, 0, self.Zhub])[:,None] # PRP to tower-shaft intersection point
            P2 = np.matmul(R_ptfm, P2) + np.array(r_ptfm)[:,None]
          
            # drawing airfoils                            
            if airfoils:
                for ii in range(m-1):
                    ax.plot(P2[0, npts*ii:npts*(ii+1)], P2[1, npts*ii:npts*(ii+1)], P2[2, npts*ii:npts*(ii+1)], color=color, lw=0.4)  
            # draw outline
            ax.plot(P2[0, 0:-1:npts], P2[1, 0:-1:npts], P2[2, 0:-1:npts], color=color, lw=0.4, zorder=2) # leading edge  
            ax.plot(P2[0, 2:-1:npts], P2[1, 2:-1:npts], P2[2, 2:-1:npts], color=color, lw=0.4, zorder=2)  # trailing edge
            
            
        #for j in range(m):
        #    linebit.append(ax.plot(Xs[j::m], Ys[j::m], Zs[j::m]            , color='k'))  # station rings
        #
        #return linebit

    
    def IECKaimal(self, case, current=False):        # 
        '''Calculates rotor-averaged turbulent wind spectrum based on inputted turbulence intensity or class.'''
        
        #TODO: expand commenting, confirm that Rot is power spectrum, skip V,W calcs if not used

        if current:
            speed = getFromDict(case, 'current_speed', shape=0, default=1.0)
            turbulence = getFromDict(case, 'current_turbulence', shape=0, default=0.0)
        else:
            speed = getFromDict(case, 'wind_speed', shape=0, default=10.0)
            turbulence = getFromDict(case, 'turbulence', shape=0, default=0.0)

        # Set inputs (f, V_ref, HH, Class, Categ, TurbMod, R)
        f = self.w / 2 / np.pi    # frequency in Hz
        HH = abs(self.Zhub)     # <<< Temporary absolute value to avoid NaNs with underwater turbines. Eventually need a new function <<<
        R = self.R_rot
        V_ref = speed
        
        ###### Initialize IEC Wind parameters #######
        iec_wind = pyIECWind_extreme()
        iec_wind.z_hub = HH
        
        if isinstance(turbulence,str):
            # If a string, the options are I, II, III, IV
            Class = ''
            for char in turbulence:
                if char == 'I' or char == 'V':
                    Class += char
                else:
                    break
            
            if not Class:
                raise Exception(f"Turbulence class must start with I, II, III, or IV: case['turbulence'] = {turbulence}")
            else:
                Categ = char
                iec_wind.Turbulence_Class = Categ

            try:
                TurbMod = turbulence.split('_')[1]
            except:
                raise Exception(f"Error reading the turbulence model: {turbulence}")

            iec_wind.Turbine_Class = Class
        
        # set things up (use default values if not specified in the above)
        iec_wind.setup()
        
        # Can set iec_wind.I_ref here if wanted, NTM used then
        if isinstance(turbulence,int):
            turbulence = float(turbulence)
        if isinstance(turbulence,float):
            iec_wind.I_ref = turbulence    # this overwrites the value set in setup method
            TurbMod = 'NTM'

        # Compute wind turbulence standard deviation (invariant with height)
        if TurbMod == 'NTM':
            sigma_1 = iec_wind.NTM(V_ref)
        elif TurbMod == 'ETM':
            sigma_1 = iec_wind.ETM(V_ref)
        elif TurbMod == 'EWM':
            sigma_1 = iec_wind.EWM(V_ref)[0]
        else:
            raise Exception("Wind model must be either NTM, ETM, or EWM. While you wrote " + TurbMod)

        # Compute turbulence scale parameter Annex C3 of IEC 61400-1-2019
        # Longitudinal
        if HH <= 60:
            L_1 = .7 * HH
        else:
            L_1 = 42.
        sigma_u = sigma_1
        L_u = 8.1 * L_1
        # Lateral
        sigma_v =  0.8 * sigma_1
        L_v = 2.7 * L_1 
        # Upward
        sigma_w =  0.5 * sigma_1
        L_w = 0.66 * L_1 

        U = (4*L_u/V_ref)*sigma_u**2/((1+6*f*L_u/V_ref)**(5./3.))
        V = (4*L_v/V_ref)*sigma_v**2/((1+6*f*L_v/V_ref)**(5./3.))
        W = (4*L_w/V_ref)*sigma_w**2/((1+6*f*L_w/V_ref)**(5./3.))

        kappa = 12 * np.sqrt((f/V_ref)**2 + (0.12 / L_u)**2)

        Rot = (2*U / (R * kappa)**3) * \
            (modstruve(1,2*R*kappa) - iv(1,2*R*kappa) - 2/np.pi + \
                R*kappa * (-2 * modstruve(-2,2*R*kappa) + 2 * iv(2,2*R*kappa) + 1) )

        # set NaNs to 0
        Rot[np.isnan(Rot)] = 0
        
        # Formulas from Section 6.3 of IEC 61400-1-2019
        # S_1_f = 0.05 * sigma_1**2. * (L_1 / V_hub) ** (-2./3.) * f **(-5./3)
        # S_2_f = S_3_f = 4. / 3. * S_1_f
        # sigma_k = np.sqrt(np.trapz(S_1_f, f))
        # print(sigma_k)
        # print(sigma_u)

        return U, V, W, Rot


if __name__=='__main__':
    fname_design = os.path.join(raft_dir,'designs/VolturnUS-S.yaml')

    # open the design YAML file and parse it into a dictionary for passing to raft
    with open(fname_design) as file:
        design = yaml.load(file, Loader=yaml.FullLoader)

    # transfer some dictionary contents that would normally be done higher up in RAFT
    design['turbine']['rho_air' ] = design['site']['rho_air']
    design['turbine']['mu_air'  ] = design['site']['mu_air']
    design['turbine']['shearExp'] = design['site']['shearExp']
    
    # zero the nacelle velocity feedback gain since there seems to be a discrepancy with its definition
    design['turbine']['pitch_control']['Fl_Kp'] = 0.0


    UU = np.arange(6,17,2)           # wind speeds
    ws = np.arange(0.01,6.0,0.01) # frequencies (rad/s)

    # make Rotor object
    rotor = Rotor(design['turbine'], ws)    
    
    
    

    import matplotlib.pyplot as plt
    import matplotlib as mpl
    from matplotlib import cm
    
    cmapper = cm.get_cmap('inferno_r')

    fig,ax = plt.subplots(2,2,sharex=True, figsize=(10,4.8))
    
    # loop through each case    
    for i_case in range(len(UU)):
    
        # manually set the case
        case = dict(wind_speed    = UU[i_case], 
                    wind_heading  = 0, 
                    turbulence    = 'IB_NTM', 
                    turbine_status= 'operating', 
                    yaw_misalign  = 0, 
                    wave_spectrum = 'JONSWAP', 
                    wave_period   = 8, 
                    wave_height   = 2, 
                    wave_heading  = 0 )
        
    
        print(f"  Running case {i_case}")
        
        F_aero0, f_aero, a_aero, b_aero = rotor.calcAeroServoContributions(case)


        rgba = cmapper((i_case+1)/8)

        ax[0,0].plot(ws/2.0/np.pi, a_aero              , color=rgba, label=f"U = {UU[i_case]:2.0f} m/s")
        ax[1,0].plot(ws/2.0/np.pi, b_aero              , color=rgba, label=f"U = {UU[i_case]:2.0f} m/s")
        ax[0,1].plot(ws/2.0/np.pi, np.real(rotor.c_exc), color=rgba)
        ax[0,1].plot(ws/2.0/np.pi, np.imag(rotor.c_exc), color=rgba, ls=":") 
        ax[1,1].plot(ws/2.0/np.pi, rotor.V_w           , color=rgba, label=f"U = {UU[i_case]:2.0f} m/s")

    
    ax[0,1].plot([],[], color=[0.5,0.5,0.5],        label='real')
    ax[0,1].plot([],[], color=[0.5,0.5,0.5], ls=":",label='imaginary')

    ax[0,0].set_ylabel(r"$a_{aero}(\omega)$ (kg)")  
    ax[1,0].set_ylabel(r"$b_{aero}(\omega)$ (Ns/m)") 
    ax[0,1].set_ylabel(r"$H_{Uf}(\omega)$ (Ns/m)") 
    ax[1,1].set_ylabel(r"$U(\omega)$ (m/s)") 
    ax[1,0].set_xlabel(r"frequency (Hz)") 
    ax[1,1].set_xlabel(r"frequency (Hz)") 
    ax[1,1].set_xlim([0,0.2]) 
    
    # force to use exponent y axis labeling
    ax[0,0].ticklabel_format(axis='y', scilimits=[-3, 3])
    ax[1,0].ticklabel_format(axis='y', scilimits=[-3, 3])
    ax[0,1].ticklabel_format(axis='y', scilimits=[-3, 3])
    ax[1,1].ticklabel_format(axis='y', scilimits=[-3, 3])
    
    ax[1,0].set_xticks(np.arange(0, 0.21,0.05))
    
    ax[0,0].grid()
    ax[1,0].grid()
    ax[0,1].grid()
    ax[1,1].grid()
    
    ax[0,0].legend()
    ax[0,1].legend()
    fig.align_ylabels()
    
    fig.tight_layout()
    fig.savefig("control.png", dpi=200)
    plt.show()

    # ax1[0].plot(ww,a_aer)
    # ax1[0].set_ylabel('a_aer')

    # ax1[1].plot(ww,b_aer)
    # ax1[1].set_ylabel('b_aer')

    # fig1.legend(('gains * 0','gains * 1','gains * 2'))

    # ax1[1].set_xlabel('frequency (rad/s)')

    # ax2[0].plot(ww,np.abs(C))
    # ax2[0].set_ylabel('mag(C)')

    # ax2[1].plot(ww,np.angle(C))
    # ax2[1].set_ylabel('phase(C)')

