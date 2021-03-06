import sys
import getopt
import re
import struct
import math
import numpy
import scipy
import scipy.interpolate

from application.lib.instrum_classes import *
from application.helpers.instrumentsmanager import Manager
if 'macros.iq_level_optimization' in sys.modules:
    reload(sys.modules["macros.iq_level_optimization"])

from macros.iq_level_optimization import IqOptimization
from application.lib.datacube import Datacube


class WaveformException(Exception):
    pass


class QubitException(Exception):
    pass


def gaussianFilter(x, cutoff=0.5):
    return numpy.exp(-numpy.power(numpy.fabs(numpy.real(x)) / cutoff, 2.0))


def gaussianPulse(length=500, delay=0, flank=4, normalize=True, resolution=1, filterFrequency=0.2):
    waveform = numpy.zeros((math.ceil(flank * 2) + 1 + int(math.ceil(length)) +
                            math.ceil(delay)) * int(1.0 / resolution), dtype=numpy.complex128)
    if length == 0:
        return waveform
    for i in range(0, len(waveform)):
        t = float(i) * resolution
        if t <= flank + delay:
            waveform[
                i] = numpy.exp(-0.5 * math.pow(float(t - delay - flank) / float(flank) * 3.0, 2.0))
        elif t >= flank + delay + length:
            waveform[i] = numpy.exp(-0.5 * math.pow(float(t -
                                                          delay - flank - length) / float(flank) * 3.0, 2.0))
        else:
            waveform[i] = 1.0
    pulseFFT = numpy.fft.rfft(waveform)
    freqs = numpy.linspace(0, 1.0, len(pulseFFT))
    filteredPulseFFT = pulseFFT
    filteredPulse = numpy.array(numpy.fft.irfft(
        filteredPulseFFT, len(waveform)), dtype=numpy.complex128)
    filteredPulse = waveform
    integral = numpy.sum(filteredPulse)
    if normalize:
        filteredPulse /= integral / float(length) * resolution
    return filteredPulse


def gaussianPulse2(length=500, delay=0, flank=4, normalize=True, resolution=1, filterFrequency=0.2, withDerivative=False):
    from math import ceil, floor
    gaussianLength = math.ceil(math.ceil(flank * 2) + 1)
    smallPulse = numpy.zeros(gaussianLength + delay, dtype=numpy.complex128)
    smallPulseDerivative = numpy.zeros(
        gaussianLength + delay, dtype=numpy.complex128)
    for i in range(0, len(smallPulse) - int(delay)):
        smallPulse[
            delay + i] = numpy.exp(-0.5 * math.pow(float(i - flank) / float(flank) * 3.0, 2.0))
        smallPulseDerivative[delay + i] = -1 * float(i - flank) * math.pow(1 / float(
            flank) * 3.0, 2.0) * numpy.exp(-0.5 * math.pow(float(i - flank) / float(flank) * 3.0, 2.0))
    area = sum(smallPulse)
    if area > length:
        if withDerivative:
            return (smallPulse * float(length) / area, smallPulseDerivative * float(length) / area)
        else:
            return smallPulse * float(length) / area
    else:
        plateauLength = math.ceil(length - area)
        longPulse = numpy.zeros(
            delay + plateauLength + gaussianLength, dtype=numpy.complex128)
        longPulseDerivative = numpy.zeros(
            delay + plateauLength + gaussianLength, dtype=numpy.complex128)
        longPulse[delay:] = 1.0
        longPulseDerivative[delay:] = 0
        longPulse[delay:delay + ceil(gaussianLength / 2)
                  ] = smallPulse[delay:delay + ceil(gaussianLength / 2)]
        longPulseDerivative[delay:delay + ceil(gaussianLength / 2)] = smallPulseDerivative[
            delay:delay + ceil(gaussianLength / 2)]
        longPulse[-ceil(gaussianLength / 2):] = smallPulse[delay + floor(gaussianLength / 2):]
        longPulseDerivative[-ceil(gaussianLength / 2):] = smallPulseDerivative[
            delay + floor(gaussianLength / 2):]
        if withDerivative:
            return (longPulse * float(length) / sum(longPulse), longPulseDerivative * float(length) / sum(longPulse))
        else:
            return longPulse * float(length) / sum(longPulse)


class Pulse:

    def __init__(self, shape, delay):
        self._shape = shape
        self._delay = delay

    def shape(self):
        return self._shape

    def delay(self):
        return self._delay

    def __len__(self):
        return len(self._shape) + self._delay


class PulseSequence:

    def __init__(self):
        self._base = dict()
        self._pulses = []
        self._offset = 0
        self._pos = 0

    def setOffset(self, offset):
        self._offset = offset

    def offset(self):
        return self._offset

    def setPosition(self, pos):
        self._pos = pos

    def position(self):
        return self._pos

    def addPulse(self, shape, delay=0, position=None):
        if position is None:
            pos = delay + self._pos
        else:
            pos = position + delay
        pulse = Pulse(shape, pos)
        self._pulses.append(pulse)
        self._pos = pos + len(shape)

    def addWait(self, length):
        self._pos += length

    def __len__(self):
        maxlen = 0
        for pulse in self._pulses:
            if len(pulse.shape()) + pulse.delay() > maxlen:
                maxlen = len(pulse.shape()) + pulse.delay()
        return int(max(maxlen, self._pos))

    def clearPulses(self):
        self._pulses = []

    def getWaveform(self, dataType=numpy.complex128, endAt=None):
        waveform = numpy.zeros(len(self), dtype=dataType) + self._offset
        for pulse in self._pulses:
            totalLength = len(pulse.shape()) + pulse.delay()
            if totalLength > len(waveform):
                oldLength = len(waveform)
                waveform.resize(totalLength)
                waveform[oldLength:] = self._offset
            waveform[math.ceil(pulse.delay()):math.ceil(
                pulse.delay()) + len(pulse.shape())] += pulse.shape()
        if endAt is None:
            return waveform
        if endAt < len(waveform):
            raise Exception("Waveform is too long!")
        shiftedWaveform = numpy.zeros(endAt, dtype=dataType)
        shiftedWaveform[endAt - len(self):] = waveform
        return shiftedWaveform

# This is a virtual instrument representing a Quantum Bit.
# This class provides convenience functions for setting the Qubit parameters and for
# making measurements of the Qubit state.


class Instr(Instrument):

    def parameters(self):
        return self._params

    def saveState(self, name):
        params = copy.deepcopy(self._params)
        params['waveformData'] = copy.deepcopy(self._waveforms)
        for key in params["waveformData"]:
            params["waveformData"][key] = numpy.array(
                params["waveformData"][key]).tolist()
        return params

    def restoreState(self, state):
        self._params = state
        if 'drive.frequency' in state:
            self.setDriveFrequency(state['drive.frequency'])
        if 'drive.amplitude.I' in state and 'drive.amplitude.Q' in state:
            self.setDriveAmplitude(I=state['drive.amplitude.I'], Q=state[
                                   'drive.amplitude.Q'])
        if 'drive.state' in state:
            if state['drive.state'] == True:
                self.turnOnDrive()
            else:
                self.turnOffDrive()
        if 'waveforms' in state:
            if 'fluxline' in state['waveformData']:
                print "Restoring fluxline waveform..."
                self.loadFluxlineWaveform(numpy.array(state['waveformData']['fluxline']), compensateResponse=self._params[
                                          "fluxline.compensateResponse"], factor=self._params["fluxline.compensationFactor"], samplingInterval=self._params["fluxline.samplingInterval"])
            if 'drive' in state['waveformData']:
                print "Restoring drive waveform..."
                self.loadWaveform(state['waveformData'][
                                  'drive'], readout=state['readout'])
        self._waveforms = state['waveformData']
        del self._params['waveformData']

    def turnOnDrive(self):
        self._mwg.turnOn()

    def turnOffDrive(self):
        self._mwg.turnOff()

    def driveFrequency(self):
        return self._mwg.frequency()

    def setDriveFrequency(self, f):
        """
        Changes the drive frequency of the qubit and adjust the AWG minimum voltages on the IQ channels to minimze the power leakage through the IQ mixer.
        """

        self._params["drive.frequency"] = float(f)
        self._mwg.setFrequency(f)
        if self._optimizer is not None:
            self._awg.setOffset(
                self._awgChannels[0], self._optimizer.iOffset(f))
            self._awg.setOffset(
                self._awgChannels[1], self._optimizer.qOffset(f))

    def setDrivePower(self, I=None, Q=None):
        f = self._mwg.frequency()
        aI = None
        aQ = None
        if I is not None:
            if self._optimizer is not None:
                frequencies = self._optimizer.iqPowerCalibrationData().column("frequency")
                coeffs = self._optimizer.iqPowerCalibrationData().column("coeffI")
                iInt = scipy.interpolate.interp1d(frequencies, coeffs)
                cI = iInt(f)
                aI = math.sqrt(math.pow(10.0, I / 10.0) / cI)
            else:
                aI = I
        if Q is not None:
            if self._optimizer is not None:
                frequencies = self._iqPowerCalibration.column("frequency")
                coeffs = self._iqPowerCalibration.column("coeffQ")
                qInt = scipy.interpolate.interp1d(frequencies, coeffs)
                cQ = qInt(f)
                aQ = math.sqrt(math.pow(10.0, Q / 10.0) / cQ)
            else:
                aQ = Q
        self.setDriveAmplitude(I=aI, Q=aQ)

    def setDriveAmplitude(self, **kwargs):
        channels = {"I": 0, "Q": 1}
        for arg in ["I", "Q"]:
            if arg in kwargs and kwargs[arg] is not None:
                self._params["drive.amplitude.%s" % arg] = kwargs[arg]
                offset = self._awg.offset(self._awgChannels[channels[arg]])
                self._awg.setAmplitude(
                    self._awgChannels[channels[arg]], kwargs[arg])
                diff = math.fabs(self._awg.amplitude(
                    self._awgChannels[channels[arg]]) - kwargs[arg])
                if diff > 0.001:
                    raise QubitException("AWG voltage chosen too high: Got %g V instead of %g V, %g" % (
                        self._awg.high(self._awgChannels[channels[arg]]), minValue + kwargs[arg], diff))
                if self._awg.offset(self._awgChannels[channels[arg]]) != offset:
                    raise QubitException("AWG minimum voltage has been changed: Got %g V instead of %g V" % (
                        self._awg.low(self._awgChannels[channels[arg]]), minValue))
        return True

    def fluxlineBaseWaveform(self):
        if 'fluxline.base' in self._waveforms:
            return self._waveforms["fluxline.base"]
        return []

    def generateZPulse(self, length=None, phase=None, delay=0, height=None, gaussian=True, flank=3):
        """
        Generates a z pulse
        """
        if height is None:
            height = 1.0
        if (length is None and phase is None) or (length is not None and phase is not None):
            raise QubitException(
                "Error in generateZPulse: You must specify either the length or the phase of the pulse!")
        if phase is not None:
            if not "pulses.xy.t_pi" in self._params:
                raise QubitException(
                    "Error: Length of pi pulse is not calibrated!")
            height = self._params["pulses.z.drive_amplitude"]
            length = phase * self._params["pulses.z.t_pi"] / math.pi
            if length < 0:
                height = -height
                length = -length
        if gaussian:
            pulse = gaussianPulse(length, delay=delay,
                                  flank=flank, normalize=False)
        else:
            pulse = numpy.zeros((length + delay))
            pulse[delay:delay + length] = 1.0
        return pulse * height

    def generateRabiPulse(self, length=None, phase=None, delay=0, height=1.0, gaussian=True, flank=3, f_c=None, f_sb=0, angle=0, sidebandDelay=None, f_shift=None):
        """
        Generates a Rabi pulse
        """
        if f_shift is None:
            if 'pulses.xy.f_shift' in self.parameters():
                f_shift = self.parameters()["pulses.xy.f_shift"]
            else:
                f_shift = 0
        f_sb += f_shift
        seq = PulseSequence()
        if sidebandDelay is None:
            sidebandDelay = delay
        if (length is None and phase is None) or (length is not None and phase is not None):
            raise QubitException(
                "Error: You must specify either the length or the phase of the pulse!")
        if phase is not None:
            if phase < 0:
                phase = -phase
                angle += math.pi
            if not "pulses.xy.t_pi" in self._params:
                raise QubitException(
                    "Error: Length of pi pulse is not calibrated!")
            length = phase * self._params["pulses.xy.t_pi"] / math.pi
        elif length < 0:
            length = -length
            angle += math.pi
        if gaussian:
            (pulse, deriv) = gaussianPulse2(
                length, delay=delay, flank=flank, withDerivative=True)
        else:
            pulse = numpy.zeros(max(1, (length + delay)),
                                dtype=numpy.complex128)
            pulse[delay:delay + length] = height
        pulse *= numpy.exp(1.0j * (angle + self._params["driveRotation"]))
        if "pulses.xy.useDrag" in self.parameters() and self.parameters()["pulses.xy.useDrag"] and ("frequencies.f12" in self.parameters() and self.parameters()['frequencies.f12'] is not None) and ("frequencies.f01" in self.parameters() and self.parameters()['frequencies.f01'] is not None):
            print "Using DRAG to correct pulse shape..."
            #deriv = numpy.zeros(len(pulse),dtype = numpy.complex128)
            #deriv[1:-1] = (pulse[2:]-pulse[:-2])/2.0
            #deriv[0] = pulse[1]/2.0
            #deriv[-1] = -pulse[-1]/2.0
            #delta = (f_12-f_01)*2*pi
            delta = (self.parameters()[
                     "frequencies.f12"] - self.parameters()["frequencies.f01"]) * 2.0 * math.pi
            if "pulses.xy.drag_factor" in self.parameters():
                drag_factor = self.parameters()["pulses.xy.drag_factor"]
            else:
                drag_factor = 1
            # We add Y_drag = -dX/dt/2Delta and X_Drag = dY/dt/2Delta on the
            # quadratures, scaled by drag_factor
            pulse += -(numpy.real(deriv) * 1j - numpy.imag(deriv)) / \
                delta / 2.0 * drag_factor
        elif "pulses.xy.useDrag" in self.parameters() and self.parameters()["pulses.xy.useDrag"]:
            print "Warning: DRAG pulses deactivated because f12 is not defined!"
        if f_sb != 0:
            if f_c is None:
                f_c = self.driveFrequency()
            if self._optimizer is not None:
                sidebandPulse = self._optimizer.generateCalibratedSidebandWaveform(
                    f_c=f_c, f_sb=f_sb, length=len(pulse) - delay, delay=sidebandDelay)
            else:
                sidebandPulse = exp(
                    1j * 2. * math.pi * f_sb * (arange(0, len(pulse) - delay) + sidebandDelay))
            pulse[delay:] *= sidebandPulse
        seq.addPulse(pulse)
        return seq.getWaveform()

    def loadRabiPulse(self, length=None, phase=None, delay=0, height=1.0, gaussian=True, flank=3, f_sb=None, angle=0, sidebandDelay=0, f_shift=None):
        """
        Loads a Rabi pulse into the AWG memory.
        """
        if f_sb is None and 'pulses.xy.f_sb' in self.parameters():
            f_sb = self.parameters()['pulses.xy.f_sb']
        elif f_sb is None:
            raise QubitException(
                "Error: You must specify a sideband frequency via parameter f_sb!")
        seq = PulseSequence()
        pulseLength = len(self.generateRabiPulse(length=length, phase=phase, height=height,
                                                 gaussian=gaussian, flank=flank, f_sb=f_sb, angle=angle, f_shift=f_shift))
        seq.addPulse(self.generateRabiPulse(length=length, phase=phase, height=height,
                                            gaussian=gaussian, flank=flank, f_sb=f_sb, angle=angle, f_shift=f_shift))
        seq.addWait(delay)
        return self.loadWaveform(seq.getWaveform())

    def loadRamseyPulse(self, delay=0, interval=0, height=1.0, piLength=100, spinEcho=False, flank=1, readoutDelay=0):
        """
        Loads a Ramsey waveform into the AWG memory.
        """
        piOver2Pulse = gaussianPulse(piLength / 2.0, flank=flank)
        piPulse = gaussianPulse(piLength, flank=flank)
        pulse = numpy.zeros(len(piOver2Pulse) * 2 + interval + delay) - 1.0
        pulse[delay:len(piOver2Pulse) + delay] = piOver2Pulse
        pulse[delay + len(piOver2Pulse) + interval:] = piOver2Pulse
        return self.loadWaveform(pulse, readout=len(piOver2Pulse) * 2.0 + delay + interval + readoutDelay)

    def fluxlineWaveform(self):
        if "fluxline" in self._waveforms:
            return self._waveforms["fluxline"]
        return []

    def driveRotation(self):
        return self._params["driveRotation"]

    def setDriveRotation(self, rotation):
        self._params["driveRotation"] = rotation

    def loadFluxlineWaveform(self, waveform, compensateResponse=True, samplingInterval=1.0, factor=1.0):
        self._waveforms["fluxline"] = numpy.array(waveform)
        self._params["fluxline.compensateResponse"] = compensateResponse
        self._params["fluxline.compensationFactor"] = float(factor)
        self._params["fluxline.samplingInterval"] = float(samplingInterval)
        if compensateResponse:
            return self.loadCompensatedFluxlineWaveform(waveform, samplingInterval, factor)
        return self.loadPlainFluxlineWaveform(waveform, samplingInterval)

    def loadCompensatedFluxlineWaveform(self, waveform, samplingInterval=1.0, factor=1.0, simulate=False):
        if self._fluxlineResponse is None:
            raise QubitException("No fluxline calibration data available!")
        if samplingInterval < 0.5:
            raise QubitException("Waveform sampling interval too small!")
        finalLength = 1
        while finalLength < len(waveform) + 1000:
            finalLength = finalLength << 1
        fullWaveform = numpy.zeros((finalLength)) - 1.0
        fullWaveform[:len(waveform)] = waveform
        fullWaveform[len(waveform):] = waveform[-1]

        frequencies = numpy.linspace(
            0, 1.0 / samplingInterval, finalLength / 2 + 1)
        interpolation = scipy.interpolate.interp1d(self._fluxlineResponse["frequency"], self._fluxlineResponse.column(
            "response_dac") * self._fluxlineResponse.column("response_input_sample"))
        responseFunction = interpolation(frequencies)

        correctedWaveform = numpy.fft.irfft(numpy.fft.rfft(fullWaveform) / numpy.power(
            responseFunction / gaussianFilter(frequencies, cutoff=0.45), factor))
        correctedWaveform[0] = waveform[0]
        correctedWaveform[-1] = waveform[-1]

        if max(correctedWaveform) > 1.0 or min(correctedWaveform) < -1.0:
            raise QubitException("Corrected waveform exceeds maximum range!")
        if simulate:
            return correctedWaveform
        self.loadPlainFluxlineWaveform(correctedWaveform, samplingInterval)
        return correctedWaveform

    def loadPlainFluxlineWaveform(self, waveform, samplingInterval=1.0, useAWG=True):
        """
        Loads a waveform into the Qubit's fluxline buffer.
        One point of the waveform corresponds to 0.5 ns.
        """
        if useAWG:
            if samplingInterval != 1.0:
                raise QubitException(
                    "Sampling interval for fluxline waveform must be 1 ns when using the AWG for generating the fluxline signal!")
            if len(waveform) > self._params["repetitionPeriod"] - 1000 - self._params["driveWaitTime"]:
                raise QubitException("Fluxline waveform is too long! A maximum length of %g ns is allowed. To increase this value you need to change the repition period of the master AWG or the qubit parameter 'driveWaitTime'." % (
                    self._params["repetitionPeriod"] - 1000 - self._params["driveWaitTime"]))
            if self._params["driveWaitTime"] - self._params["fluxlineTriggerDelay"] < 0:
                raise QubitException("Qubit Parameter 'driveWaitTime' is too small to compensate the fluxline delay as given in 'fluxlineTriggerDelay'. Choose at least %d ns of delay." % self._params[
                                     "fluxlineTriggerDelay"])
            markers = numpy.zeros(
                self._params["repetitionPeriod"] - 1000, dtype=numpy.uint8) + 3
            fullWaveform = numpy.zeros(
                self._params["repetitionPeriod"] - 1000) + waveform[0]
            fullWaveform[self._params["driveWaitTime"] - self._params["fluxlineTriggerDelay"]:len(
                waveform) + self._params["driveWaitTime"] - self._params["fluxlineTriggerDelay"]] = waveform
            waveformData = self._awg.writeIntData(
                (fullWaveform + 1.0) / 2.0 * ((1 << 14) - 1), markers)
            self._fluxline.createWaveform(
                self._params["fluxlineWaveform"], waveformData, "INT")
            self._fluxline.setWaveform(
                self._params["fluxlineChannel"], self._params["fluxlineWaveform"])

        else:
            delay = self._params["fluxlineWaitTime"] * 2

            transformed = numpy.zeros((max(len(waveform), 150))) - 1.0
            transformed[:len(waveform)] = waveform
            transformed[len(waveform):] = waveform[-1]
            transformed = (transformed + 1.0) * ((1 << 14) / 2.0 - 1.0)
            # If the data is sampled at 0.5 ns we just take it as it is. If not
            # we interpolate.
            if samplingInterval == 0.5:
                fullWaveform = numpy.zeros(
                    (len(transformed) + delay), dtype=numpy.uint16)
                fullWaveform[delay:] = transformed
                fullWaveform[:delay] = transformed[0]
            else:
                multiplier = int(samplingInterval / 0.5)
                fullWaveform = numpy.zeros(
                    (multiplier * (len(transformed) - 1) + 1 + delay), dtype=numpy.uint16)
                for i in range(0, len(transformed) - 1):
                    for j in range(0, multiplier):
                        fullWaveform[delay + i * multiplier + j] = transformed[i] + (
                            transformed[i + 1] - transformed[i]) / float(multiplier) * float(j)
                fullWaveform[delay + multiplier *
                             (len(transformed) - 1)] = transformed[-1]
                fullWaveform[:delay] = transformed[0]
            self._fluxline.writeWaveform(
                self._params["fluxlineWaveform"], fullWaveform)
            self._fluxline.setWaveform(self._params["fluxlineWaveform"])
            self._fluxline.setPeriod(len(fullWaveform) / 2.0)
            self._outputWaveforms["fluxline"] = fullWaveform
            self.notify("fluxlineWaveform", fullWaveform)
            return fullWaveform

    def waveform(self):
        if 'drive' in self._outputWaveforms:
            return self._outputWaveforms["drive"]
        return None

    def loadWaveform(self, iqWaveform, readout=None, markers=None):
        """
        Loads an IQ waveform into the AWG memory.
        The "readout" parameter sets the time at which the qubit should be read out.
        """

        if len(iqWaveform) + self._params["driveWaitTime"] > self._params["readoutTime"]:
            raise WaveformException("Given waveform is too long!")
            return False

        iChannel = numpy.zeros((self._params["repetitionPeriod"]))
        qChannel = numpy.zeros((self._params["repetitionPeriod"]))
        safetyMargin = self._params["driveWaitTime"]
        offsetTime = self._params["readoutTime"] - len(iqWaveform)
        iChannel[offsetTime:offsetTime +
                 len(iqWaveform)] = numpy.real(iqWaveform)
        qChannel[offsetTime:offsetTime +
                 len(iqWaveform)] = numpy.imag(iqWaveform)

        # Flux pulse trigger and readout trigger
        if markers is None:
            iMarkers = numpy.zeros(
                (self._params["repetitionPeriod"]), dtype=numpy.uint8) + 3
            qMarkers = numpy.zeros(
                (self._params["repetitionPeriod"]), dtype=numpy.uint8) + 3
            iMarkers[1:len(iMarkers) / 2] -= 3
            qMarkers[len(qMarkers) / 2:] -= 1
        else:
            iMarkers = numpy.zeros(
                (self._params["repetitionPeriod"]), dtype=numpy.uint8)
            qMarkers = numpy.zeros(
                (self._params["repetitionPeriod"]), dtype=numpy.uint8)
            iMarkers[safetyMargin:safetyMargin +
                     len(markers)] = numpy.real(markers)
            qMarkers[safetyMargin:safetyMargin +
                     len(markers)] = numpy.real(markers)

        iData = self._awg.writeIntData(
            (iChannel + 1.0) / 2.0 * ((1 << 14) - 1), iMarkers)
        self._awg.createWaveform(self._params["waveforms"][0], iData, "INT")
        self._awg.setWaveform(
            self._awgChannels[0], self._params["waveforms"][0])

        qData = self._awg.writeIntData(
            (qChannel + 1.0) / 2.0 * ((1 << 14) - 1), qMarkers)
        self._awg.createWaveform(self._params["waveforms"][1], qData, "INT")
        self._awg.setWaveform(
            self._awgChannels[1], self._params["waveforms"][1])

        self._waveforms["drive"] = numpy.array(iqWaveform)

        self._outputWaveforms["drive"]["I"] = iChannel
        self._outputWaveforms["drive"]["Q"] = qChannel
        self._outputWaveforms["drive"]["markers"]["I"] = iMarkers
        self._outputWaveforms["drive"]["markers"]["Q"] = qMarkers

        finalIqWaveform = iChannel + 1j * qChannel

        self.notify("driveWaveform", (finalIqWaveform, iMarkers, qMarkers))

        return (finalIqWaveform, iMarkers, qMarkers)

    def driveWaveform(self):
        return self._waveforms["drive"]

    def updateWaveforms(self):
        try:
            iChannel = self._awg.getWaveform(self._params["waveforms"][0])
            qChannel = self._awg.getWaveform(self._params["waveforms"][1])
        except VisaIOError:
            print "Error when retrieving waveform data..."
        self._outputWaveforms["drive"]["I"] = iChannel._data
        self._outputWaveforms["drive"]["Q"] = qChannel._data
        self._outputWaveforms["drive"]["markers"]["I"] = iChannel._markers
        self._outputWaveforms["drive"]["markers"]["Q"] = qChannel._markers
        self.notify("driveWaveform", (self._outputWaveforms["drive"]["I"], self._outputWaveforms[
                    "drive"]["Q"], self._outputWaveforms["drive"]["markers"]))
        self._outputWaveforms["fluxline"] = self._fluxline.readWaveform(
            self._params["fluxlineWaveform"])
        self.notify("fluxlineWaveform", self._outputWaveforms["fluxline"])

    def setIqOffsetCalibration(self, calibration):
        self._optimizer.setOffsetCalibrationData(calibration)

    def setIqPowerCalibration(self, calibration):
        self._optimizer.setPowerCalibrationData(calibration)

    def Psw(self, ntimes=20, normalize=False):
        self._acqiris.bifurcationMap(ntimes=ntimes)
        if normalize:
            return (self._acqiris.Psw()[self._params["acqirisVariable"]] - 1.0 + self._params["readout.p00"]) / (-1.0 + self._params["readout.p00"] + self._params["readout.p11"])
        return self._acqiris.Psw()[self._params["acqirisVariable"]]

    def calibrateIqOffset(self, frequency=None, saveCalibration=True, changeFrequency=True):
        """
        Recalibrates the IQ offset at a given single frequency and (if requested) updates the calibration data file.
        If no frequency is given, the current drive frequency will be used.
        If changeFrequency is True, the function will change the qubit drive frequency to the given frequency after the calibration.
        """
        if frequency is None:
            frequency = self._mwg.frequency()
        self._optimizer.calibrateIQOffset([frequency])
        if saveCalibration:
            self._optimizer.offsetCalibrationData().savetxt()
        if changeFrequency:
            self.setDriveFrequency(frequency)

    def calibrateSidebandMixing(self, frequencyRange=None, saveCalibration=True):
        """
        Recalibrates the IQ offset at a given single frequency and (if requested) updates the calibration data file.
        If no frequency is given, the current drive frequency will be used.
        If changeFrequency is True, the function will change the qubit drive frequency to the given frequency after the calibration.
        """
        if frequencyRange is None:
            frequencyRange = [self._mwg.frequency()]
        self._optimizer.calibrateSidebandMixing(frequencyRange=frequencyRange)
        if saveCalibration:
            self._optimizer.sidebandCalibrationData().savetxt()

    def setFluxlineResponse(self, response):
        self._fluxlineResponse = response
        self._fluxlineResponseInterpolations = dict()

    def initialize(self, jba, pulseGenerator):
        manager = Manager()
        if not hasattr(self, '_params'):
            self._params = dict()
        self._params['jba'] = jba
        self._jba = manager.getInstrument(jba)
        self._params['pulseGenerator'] = pulseGenerator
        self._pulseGenerator = manager.getInstrument(pulseGenerator)

    def loadRabiPulse2(self, frequency, duration=250, amplitude=1., phase=0., gaussian=False, delayFromZero=None):
        if delayFromZero is None:
            delayFromZero = register.parameters()['readoutDelay'] - duration
        self._pulseGenerator.generatePulse(frequency=frequency, duration=duration, amplitude=amplitude,
                                           phase=phase, gaussian=gaussian, allowModifyMWFrequency=False, delayFromZero=delayFromZero)
